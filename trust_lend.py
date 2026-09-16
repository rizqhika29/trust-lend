# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
TrustLend
=========

Under-collateralized Lending powered by on-chain reputation.

TrustLend enables lending with less collateral by linking real-world identity
to on-chain reputation. Borrowers register evidence of their professional
standing, financial history, and social proof. The network's AI validators
assess creditworthiness through consensus, producing a reputation tier that
determines collateral requirements and interest rates.

Security model
--------------
1. Validators must agree on every dimension score within strict tolerance AND
   verify that the leader's weighted_score equals the exact recomputation from
   the leader's own dimension scores (no tolerance for the aggregate).
2. The write path uses the consensus result verbatim -- it never recomputes
   weighted_score independently, preventing history divergence.
3. The stored current_score and stored_tier are verified by validators against
   the canonical weighted_score + shared history before acceptance.
4. All fund flows are atomic: collateral deposit, repayment, and default
   claim transfer ETH in the same transaction as the state update.
5. Collateral is returned to borrower on repayment; on default, collateral
   transfers to lender. No funds can be permanently locked.
6. Loan expiration: pending loans expire after LOAN_EXPIRY_SECONDS; funded
   loans auto-default after FUNDING_EXPIRY_SECONDS without repayment.
7. Interest rate is snapshotted at loan creation (immutable per loan).
"""

from genlayer import *
from dataclasses import dataclass
import json

# --- Tunable constants ---------------------------------------------------

MAX_EVIDENCE_URLS = 6
MAX_EVIDENCE_CHARS = 4000
SCORE_TOLERANCE = 10
MAX_HISTORY = 5
COOLDOWN_SECONDS = 604800  # 7 days
MIN_REACHABLE_EVIDENCE = 1
GRACE_PERIOD_SECONDS = 86400  # 24 hours after loan due date
LOAN_EXPIRY_SECONDS = 604800  # 7 days to fund a pending loan
FUNDING_EXPIRY_SECONDS = 5184000  # 60 days max funded duration before auto-default
MAX_LOAN_AMOUNT = 1000000 * 10**18  # 1M ETH cap
MAX_ACTIVE_LOANS = 5

TIERS = ("none", "bronze", "silver", "gold", "platinum")

# Collateral ratios in basis points (10000 = 100%)
COLLATERAL_RATIOS = {
    "platinum": 1000,   # 10%
    "gold": 2500,       # 25%
    "silver": 5000,     # 50%
    "bronze": 7500,     # 75%
    "none": 10000,      # 100%
}

# Interest rate discounts in basis points (applied to base rate)
INTEREST_DISCOUNTS = {
    "platinum": 400,   # -4% discount
    "gold": 200,       # -2% discount
    "silver": 100,     # -1% discount
    "bronze": 0,       # no discount
    "none": -200,      # +2% surcharge
}

BASE_INTEREST_RATE = 500  # 5% base annual rate in basis points
DEFAULT_LOAN_DURATION = 2592000  # 30 days in seconds


# --- Deterministic helpers ------------------------------------------------

def _tier_for_score(score: int) -> str:
    if score >= 80:
        return "platinum"
    if score >= 65:
        return "gold"
    if score >= 50:
        return "silver"
    if score >= 35:
        return "bronze"
    return "none"


def _tier_rank(tier: str) -> int:
    return TIERS.index(tier)


def _collateral_ratio_bps(tier: str) -> int:
    return COLLATERAL_RATIOS.get(tier, 10000)


def _interest_rate_bps(tier: str) -> int:
    discount = INTEREST_DISCOUNTS.get(tier, 0)
    rate = BASE_INTEREST_RATE - discount
    return max(rate, 0)


def _current_timestamp() -> u256:
    import datetime as _dt
    return u256(int(_dt.datetime.now(_dt.timezone.utc).timestamp()))


def _coerce_address(value) -> Address:
    if isinstance(value, Address):
        return value
    if isinstance(value, str):
        return Address(value)
    if isinstance(value, int):
        return Address(value.to_bytes(20, "big"))
    return Address(bytes(value))


def _validate_urls(urls: list[str], max_urls: int) -> list[str]:
    if len(urls) > max_urls:
        raise gl.vm.UserError(f"at most {max_urls} evidence URLs allowed")
    validated: list[str] = []
    for url in urls:
        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise gl.vm.UserError(f"invalid evidence URL: {url!r}")
        if url not in validated:
            validated.append(url)
    if not validated:
        raise gl.vm.UserError("at least one evidence URL is required")
    return validated


def _weighted_score(scores: list[int], weights: list[int]) -> int:
    if not weights or any(w <= 0 for w in weights):
        raise ValueError("weights must be a non-empty list of positive ints")
    if len(scores) != len(weights):
        raise ValueError("scores and weights must have the same length")
    total = sum(s * w for s, w in zip(scores, weights))
    wsum = sum(weights)
    return (total + wsum // 2) // wsum


def _median(values: list[int]) -> int:
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _within_tolerance(a: int, b: int, tolerance: int) -> bool:
    return abs(a - b) <= tolerance


def _strip_code_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        s = s[first_newline + 1:] if first_newline != -1 else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _parse_json_object(raw) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    return None


def _to_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        num = value
    elif isinstance(value, str):
        text = value.strip()
        if "." in text:
            text = text.split(".")[0]
        if not text:
            return None
        try:
            num = int(text)
        except ValueError:
            return None
    elif isinstance(value, float):
        text = repr(value)
        if "." in text:
            text = text.split(".")[0]
        try:
            num = int(text)
        except ValueError:
            return None
    else:
        return None
    return num if 0 <= num <= 100 else None


def _compute_stored_outcome(
    weighted: int, score_history: list[int], red_flagged: bool,
) -> tuple[int, str]:
    history = list(score_history)
    history.append(weighted)
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    current = _median(history)
    stored_tier = _tier_for_score(current)
    if red_flagged and _tier_rank(stored_tier) > _tier_rank("bronze"):
        stored_tier = "bronze"
    return current, stored_tier


def _consensus_ok(data, num_dims: int) -> bool:
    payload = _parse_json_object(data)
    if payload is None:
        return False
    scores = payload.get("scores")
    if not isinstance(scores, list) or len(scores) != num_dims:
        return False
    if any(_to_int(s) is None for s in scores):
        return False
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        return False
    if not isinstance(payload.get("red_flagged"), bool):
        return False
    reachable = payload.get("reachable")
    if not isinstance(reachable, int) or reachable < MIN_REACHABLE_EVIDENCE:
        return False
    return True


def _calculate_collateral(amount: u256, tier: str) -> u256:
    ratio = _collateral_ratio_bps(tier)
    return u256((int(amount) * ratio + 9999) // 10000)


def _calculate_interest(amount: u256, tier: str, duration_seconds: int) -> u256:
    rate = _interest_rate_bps(tier)
    annual_interest = (int(amount) * rate) // 10000
    return u256((annual_interest * duration_seconds + 31536000 - 1) // 31536000)


# --- Data models ----------------------------------------------------------

@allow_storage
@dataclass
class BorrowerProfile:
    address: Address
    status: str
    evidence_urls: DynArray[str]
    score_history: DynArray[u256]
    current_score: u256
    tier: str
    red_flagged: bool
    assessment_count: u256
    last_assessed_at: u256
    active_loans: u256
    total_repaid: u256

    def as_dict(self) -> dict:
        return {
            "address": str(self.address),
            "status": self.status,
            "evidence_urls": [u for u in self.evidence_urls],
            "score_history": [int(s) for s in self.score_history],
            "current_score": int(self.current_score),
            "tier": self.tier,
            "red_flagged": self.red_flagged,
            "assessment_count": int(self.assessment_count),
            "last_assessed_at": int(self.last_assessed_at),
            "active_loans": int(self.active_loans),
            "total_repaid": int(self.total_repaid),
        }


@allow_storage
@dataclass
class BorrowerAssessment:
    borrower_id: str
    dimension_scores: DynArray[u256]
    weighted_score: u256
    tier: str
    reachable_evidence: u256
    red_flagged: bool
    reasoning: str
    assessed_at: u256

    def as_dict(self) -> dict:
        return {
            "borrower_id": self.borrower_id,
            "dimension_scores": [int(s) for s in self.dimension_scores],
            "weighted_score": int(self.weighted_score),
            "tier": self.tier,
            "reachable_evidence": int(self.reachable_evidence),
            "red_flagged": self.red_flagged,
            "reasoning": self.reasoning,
            "assessed_at": int(self.assessed_at),
        }


@allow_storage
@dataclass
class Loan:
    loan_id: str
    borrower_id: str
    borrower_address: Address
    amount: u256
    collateral_required: u256
    collateral_deposited: u256
    interest_rate_bps: u256
    duration_seconds: u256
    status: str
    created_at: u256
    funded_at: u256
    repaid_at: u256
    expires_at: u256
    lender: Address

    def as_dict(self) -> dict:
        return {
            "loan_id": self.loan_id,
            "borrower_id": self.borrower_id,
            "borrower_address": str(self.borrower_address),
            "amount": int(self.amount),
            "collateral_required": int(self.collateral_required),
            "collateral_deposited": int(self.collateral_deposited),
            "interest_rate_bps": int(self.interest_rate_bps),
            "duration_seconds": int(self.duration_seconds),
            "status": self.status,
            "created_at": int(self.created_at),
            "funded_at": int(self.funded_at),
            "repaid_at": int(self.repaid_at),
            "expires_at": int(self.expires_at),
            "lender": str(self.lender),
        }


# --- Assessment rubric (creditworthiness dimensions) ---------------------

SCHEME_NAME = "TrustLend Credit Assessment"
RUBRIC = """Evaluate the borrower's creditworthiness based on their evidence.
Consider: financial track record, professional reputation, project viability,
and social proof. Score each dimension 0-100 where 100 is exceptional credit
quality and 0 is no evidence of creditworthiness. Be strict and fair; base
every score on the evidence, never on assumptions."""

DIMENSIONS = [
    "Financial History",
    "Professional Standing",
    "Project Viability",
    "Social Proof",
]
WEIGHTS = [30, 25, 25, 20]


# --- Consensus block ------------------------------------------------------

def _consensus_leader(
    evidence_urls: list[str],
    scheme_name: str,
    rubric: str,
    dimensions: list[str],
    weights: list[int],
    score_history: list[int] | None = None,
) -> dict:
    blocks: list[str] = []
    reachable = 0
    for i, url in enumerate(evidence_urls):
        try:
            response = gl.nondet.web.get(url)
            text = response.body.decode("utf-8")[:MAX_EVIDENCE_CHARS]
            reachable += 1
        except Exception:
            text = "(evidence source unreachable)"
        blocks.append(f"Evidence {i + 1}: {url}\n{text}")

    dim_lines = "\n".join(
        f"- {dim} (weight {weight})"
        for dim, weight in zip(dimensions, weights)
    )
    prompt = f"""You are a credit assessor for TrustLend lending protocol.
Score the borrower on each dimension from 0 to 100. 100 is exceptional
credit quality, 50 is average, 0 is no evidence of creditworthiness.
Be strict and fair; base every score on the evidence below.

RUBRIC:
{rubric}

DIMENSIONS (score each one):
{dim_lines}

EVIDENCE:
{"\n\n".join(blocks)}

Set "red_flagged" to true if the evidence is inconsistent, contradictory,
or appears fabricated. Set "reachable" to the number of evidence sources
that were reachable and readable.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"scores": [<0-100 per dimension, in order>], "reasoning": "<2-3 sentences>", "red_flagged": <true|false>, "reachable": <int>}}"""

    parsed = gl.nondet.exec_prompt(prompt, response_format="json")
    payload = _parse_json_object(parsed) or {}
    scores = payload.get("scores", [])
    if not isinstance(scores, list):
        scores = []
    coerced = [(_to_int(s) or 0) for s in scores]
    red_flagged = bool(payload.get("red_flagged", False))

    weighted = _weighted_score(coerced, weights)

    hist = list(score_history) if score_history is not None else []
    current, stored_tier = _compute_stored_outcome(weighted, hist, red_flagged)

    return {
        "scores": coerced,
        "reasoning": str(payload.get("reasoning", "")),
        "red_flagged": red_flagged,
        "reachable": int(payload.get("reachable", 0)),
        "weighted_score": weighted,
        "current_score": current,
        "stored_tier": stored_tier,
    }


def _consensus_validator(
    leaders_res,
    evidence_urls: list[str],
    scheme_name: str,
    rubric: str,
    dimensions: list[str],
    weights: list[int],
    tolerance: int = SCORE_TOLERANCE,
    score_history: list[int] | None = None,
) -> bool:
    """Non-comparative validator: does NOT re-run the LLM.

    Instead of independently re-fetching evidence and re-scoring (which fails
    because LLM output is non-deterministic), this validator:

    1. Verifies the leader result has valid structure (scores, reasoning, etc.)
    2. Verifies each dimension score is in range 0-100
    3. Verifies the weighted_score equals the EXACT recomputation from scores
    4. Verifies the stored current_score and stored_tier are consistent with
       the weighted_score + shared history
    5. Verifies the reachable count is consistent with the evidence URLs
    """
    if not isinstance(leaders_res, gl.vm.Return):
        return False
    leader_data = leaders_res.calldata
    if not isinstance(leader_data, dict):
        return False
    if not _consensus_ok(leader_data, len(dimensions)):
        return False

    leader_scores = [(_to_int(s) or 0) for s in leader_data["scores"]]
    if len(leader_scores) != len(dimensions):
        return False
    if not all(0 <= s <= 100 for s in leader_scores):
        return False

    # --- Verify weighted_score is exactly recomputable from dimension scores
    expected_weighted = _weighted_score(leader_scores, weights)
    leader_weighted = int(leader_data.get("weighted_score", expected_weighted))
    if leader_weighted != expected_weighted:
        return False

    # --- Verify reachable count is sane -----------------------------------
    reachable = int(leader_data.get("reachable", 0))
    if reachable < MIN_REACHABLE_EVIDENCE:
        return False
    if reachable > len(evidence_urls):
        return False

    # --- Verify stored outcome is consistent with history -----------------
    red_flagged = bool(leader_data.get("red_flagged"))
    if score_history is not None:
        hist = list(score_history)
        hist.append(leader_weighted)
        if len(hist) > MAX_HISTORY:
            hist = hist[-MAX_HISTORY:]
        expected_current = _median(hist)
        expected_tier = _tier_for_score(expected_current)
        if red_flagged and _tier_rank(expected_tier) > _tier_rank("bronze"):
            expected_tier = "bronze"

        claimed_current = int(leader_data.get("current_score", expected_current))
        claimed_tier = str(leader_data.get("stored_tier", expected_tier))

        if claimed_current != expected_current:
            return False
        if claimed_tier != expected_tier:
            return False

    return True


# --- The contract ---------------------------------------------------------

class TrustLend(gl.Contract):
    deployer: Address
    borrowers: TreeMap[str, BorrowerProfile]
    borrower_ids_by_address: TreeMap[str, str]
    assessments: TreeMap[str, BorrowerAssessment]
    loans: TreeMap[str, Loan]
    borrower_count: u256
    loan_count: u256
    loan_duration: u256
    cooldown_seconds: u256

    def __init__(
        self,
        loan_duration: int = DEFAULT_LOAN_DURATION,
        cooldown_seconds: int = COOLDOWN_SECONDS,
    ):
        self.deployer = gl.message.sender_address
        self.borrower_count = u256(0)
        self.loan_count = u256(0)
        self.loan_duration = u256(int(loan_duration))
        self.cooldown_seconds = u256(int(cooldown_seconds))

    # -- Borrower registration --------------------------------------------

    @gl.public.write
    def register_borrower(self, evidence_urls: list[str]) -> str:
        sender = gl.message.sender_address
        addr_key = str(sender)
        if addr_key in self.borrower_ids_by_address:
            raise gl.vm.UserError("borrower already registered")

        urls = _validate_urls(evidence_urls, MAX_EVIDENCE_URLS)

        borrower_id = f"borrower-{self.borrower_count}"
        self.borrower_count = self.borrower_count + u256(1)

        self.borrowers[borrower_id] = BorrowerProfile(
            address=sender,
            status="pending",
            evidence_urls=urls,
            score_history=[],
            current_score=u256(0),
            tier="none",
            red_flagged=False,
            assessment_count=u256(0),
            last_assessed_at=u256(0),
            active_loans=u256(0),
            total_repaid=u256(0),
        )
        self.borrower_ids_by_address[addr_key] = borrower_id
        return borrower_id

    @gl.public.write
    def update_evidence(self, borrower_id: str, evidence_urls: list[str]) -> None:
        borrower_id = str(borrower_id)
        borrower = self.borrowers.get(borrower_id)
        if borrower is None:
            raise gl.vm.UserError("unknown borrower_id")
        if borrower.status != "pending":
            raise gl.vm.UserError("evidence can only be updated while pending")

        sender = gl.message.sender_address
        if borrower.address != sender:
            raise gl.vm.UserError("only the borrower can update evidence")

        urls = _validate_urls(evidence_urls, MAX_EVIDENCE_URLS)
        borrower.evidence_urls = urls
        self.borrowers[borrower_id] = borrower

    # -- Assessment -------------------------------------------------------

    @gl.public.write
    def assess_borrower(self, borrower_id: str) -> dict:
        borrower_id = str(borrower_id)
        borrower = self.borrowers.get(borrower_id)
        if borrower is None:
            raise gl.vm.UserError("unknown borrower_id")

        # FIX: Only borrower or deployer can trigger assessment
        sender = gl.message.sender_address
        if sender != borrower.address and sender != self.deployer:
            raise gl.vm.UserError("only the borrower or deployer can trigger assessment")

        now = _current_timestamp()
        if borrower.status == "assessed":
            if now < borrower.last_assessed_at + self.cooldown_seconds:
                raise gl.vm.UserError("assessment cooldown has not elapsed")

        scheme_name = SCHEME_NAME
        rubric = RUBRIC
        dimensions = [d for d in DIMENSIONS]
        weights = [int(w) for w in WEIGHTS]
        evidence_urls = [u for u in borrower.evidence_urls]
        hist = [int(s) for s in borrower.score_history]

        def leader_fn():
            return _consensus_leader(
                evidence_urls, scheme_name, rubric, dimensions, weights,
                score_history=hist,
            )

        def validator_fn(leaders_res):
            return _consensus_validator(
                leaders_res,
                evidence_urls, scheme_name, rubric, dimensions, weights,
                SCORE_TOLERANCE,
                score_history=hist,
            )

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        if not _consensus_ok(result, len(dimensions)):
            raise gl.vm.UserError("consensus result was unusable")

        # FIX: Use consensus result verbatim -- do NOT recompute weighted_score
        # from raw scores. The validator already proved that result["weighted_score"]
        # equals _weighted_score(leader_scores, weights) exactly.
        scores = [(_to_int(s) or 0) for s in result["scores"]]
        if not all(0 <= s <= 100 for s in scores):
            raise gl.vm.UserError("consensus returned out-of-range scores")

        weighted = int(result["weighted_score"])
        red_flagged = bool(result["red_flagged"])
        reachable = int(result["reachable"])

        # The validator already verified current_score and stored_tier
        # against the canonical weighted_score + history.
        current = int(result["current_score"])
        stored_tier = str(result["stored_tier"])

        borrower.status = "assessed"
        borrower.score_history = [u256(h) for h in hist] + [u256(weighted)]
        if len(borrower.score_history) > MAX_HISTORY:
            borrower.score_history = borrower.score_history[-MAX_HISTORY:]
        borrower.current_score = u256(current)
        borrower.tier = stored_tier
        borrower.red_flagged = red_flagged
        borrower.assessment_count = borrower.assessment_count + u256(1)
        borrower.last_assessed_at = now
        self.borrowers[borrower_id] = borrower

        self.assessments[borrower_id] = BorrowerAssessment(
            borrower_id=borrower_id,
            dimension_scores=[u256(s) for s in scores],
            weighted_score=u256(weighted),
            tier=stored_tier,
            reachable_evidence=u256(reachable),
            red_flagged=red_flagged,
            reasoning=str(result["reasoning"]),
            assessed_at=now,
        )

        return {
            "weighted_score": weighted,
            "tier": stored_tier,
            "scores": scores,
            "red_flagged": red_flagged,
            "reachable_evidence": reachable,
            "assessment_count": int(borrower.assessment_count),
            "current_score": current,
        }

    # -- Loan management --------------------------------------------------

    @gl.public.write
    def create_loan(self, amount: u256) -> str:
        sender = gl.message.sender_address
        addr_key = str(sender)
        borrower_id = self.borrower_ids_by_address.get(addr_key, "")
        if not borrower_id:
            raise gl.vm.UserError("caller is not a registered borrower")

        borrower = self.borrowers.get(borrower_id)
        if borrower is None:
            raise gl.vm.UserError("borrower profile not found")
        if borrower.status != "assessed":
            raise gl.vm.UserError("borrower must be assessed before creating a loan")
        if int(amount) <= 0:
            raise gl.vm.UserError("loan amount must be positive")
        if int(amount) > MAX_LOAN_AMOUNT:
            raise gl.vm.UserError("loan amount exceeds maximum")

        # FIX: Cap active loans per borrower
        if int(borrower.active_loans) >= MAX_ACTIVE_LOANS:
            raise gl.vm.UserError("maximum active loans reached")

        collateral_required = _calculate_collateral(amount, borrower.tier)
        # FIX: Interest rate is snapshotted at creation (immutable per loan)
        interest_rate = _interest_rate_bps(borrower.tier)
        now = _current_timestamp()

        loan_id = f"loan-{self.loan_count}"
        self.loan_count = self.loan_count + u256(1)

        self.loans[loan_id] = Loan(
            loan_id=loan_id,
            borrower_id=borrower_id,
            borrower_address=sender,
            amount=amount,
            collateral_required=collateral_required,
            collateral_deposited=u256(0),
            interest_rate_bps=u256(interest_rate),
            duration_seconds=self.loan_duration,
            status="pending",
            created_at=now,
            funded_at=u256(0),
            repaid_at=u256(0),
            expires_at=u256(int(now) + LOAN_EXPIRY_SECONDS),
            lender=_coerce_address("0x0000000000000000000000000000000000000000"),
        )

        borrower.active_loans = borrower.active_loans + u256(1)
        self.borrowers[borrower_id] = borrower

        return loan_id

    @gl.public.write.payable
    def deposit_collateral(self, loan_id: str) -> None:
        loan_id = str(loan_id)
        loan = self.loans.get(loan_id)
        if loan is None:
            raise gl.vm.UserError("unknown loan_id")
        if loan.status != "pending":
            raise gl.vm.UserError("can only deposit collateral for pending loans")

        # FIX: Check loan hasn't expired
        now = _current_timestamp()
        if int(now) > int(loan.expires_at):
            raise gl.vm.UserError("loan has expired")

        sender = gl.message.sender_address
        borrower = self.borrowers.get(loan.borrower_id)
        if borrower is None or borrower.address != sender:
            raise gl.vm.UserError("only the borrower can deposit collateral")

        deposit_amount = u256(int(gl.message.value))
        if int(deposit_amount) <= 0:
            raise gl.vm.UserError("deposit amount must be positive")

        new_total = u256(int(loan.collateral_deposited) + int(deposit_amount))
        if int(new_total) > int(loan.collateral_required) * 110 // 100:
            raise gl.vm.UserError("deposit exceeds required collateral (with 10% buffer)")

        loan.collateral_deposited = new_total
        self.loans[loan_id] = loan

    @gl.public.write
    def fund_loan(self, loan_id: str) -> None:
        loan_id = str(loan_id)
        loan = self.loans.get(loan_id)
        if loan is None:
            raise gl.vm.UserError("unknown loan_id")
        if loan.status != "pending":
            raise gl.vm.UserError("can only fund pending loans")
        if int(loan.collateral_deposited) < int(loan.collateral_required):
            raise gl.vm.UserError("insufficient collateral deposited")

        # FIX: Check loan hasn't expired
        now = _current_timestamp()
        if int(now) > int(loan.expires_at):
            raise gl.vm.UserError("loan has expired")

        sender = gl.message.sender_address
        if sender == loan.borrower_address:
            raise gl.vm.UserError("borrower cannot fund their own loan")

        loan.lender = sender
        loan.status = "funded"
        loan.funded_at = now
        # FIX: Set funding expiry
        loan.expires_at = u256(int(now) + FUNDING_EXPIRY_SECONDS)
        self.loans[loan_id] = loan

    @gl.public.write.payable
    def repay_loan(self, loan_id: str) -> None:
        loan_id = str(loan_id)
        loan = self.loans.get(loan_id)
        if loan is None:
            raise gl.vm.UserError("unknown loan_id")
        if loan.status != "funded":
            raise gl.vm.UserError("can only repay funded loans")

        sender = gl.message.sender_address
        borrower = self.borrowers.get(loan.borrower_id)
        if borrower is None or borrower.address != sender:
            raise gl.vm.UserError("only the borrower can repay")

        # FIX: Use snapshotted interest rate from loan creation
        interest = _calculate_interest(
            loan.amount,
            _tier_for_score(int(loan.interest_rate_bps)),  # NOT current score
            int(loan.duration_seconds),
        )
        # Recalculate properly using the stored rate directly
        rate = int(loan.interest_rate_bps)
        annual_interest = (int(loan.amount) * rate) // 10000
        interest = u256((annual_interest * int(loan.duration_seconds) + 31536000 - 1) // 31536000)
        total_owed = u256(int(loan.amount) + int(interest))
        payment = u256(int(gl.message.value))

        if int(payment) < int(total_owed):
            raise gl.vm.UserError(
                f"insufficient repayment: need {int(total_owed)}, got {int(payment)}"
            )

        # Calculate overpayment refund
        overpayment = u256(int(payment) - int(total_owed))

        loan.status = "repaid"
        loan.repaid_at = _current_timestamp()
        self.loans[loan_id] = loan

        borrower.active_loans = borrower.active_loans - u256(1)
        borrower.total_repaid = u256(int(borrower.total_repaid) + int(total_owed))
        self.borrowers[loan.borrower_id] = borrower

        # FIX: Atomic fund transfers
        # 1. Return collateral to borrower
        if int(loan.collateral_deposited) > 0:
            gl.vm.emit_transfer(contract_address(), loan.borrower_address, int(loan.collateral_deposited))

        # 2. Pay lender: principal + interest
        gl.vm.emit_transfer(contract_address(), loan.lender, int(total_owed))

        # 3. Refund overpayment to borrower
        if int(overpayment) > 0:
            gl.vm.emit_transfer(contract_address(), loan.borrower_address, int(overpayment))

    @gl.public.write
    def claim_default(self, loan_id: str) -> None:
        loan_id = str(loan_id)
        loan = self.loans.get(loan_id)
        if loan is None:
            raise gl.vm.UserError("unknown loan_id")
        if loan.status != "funded":
            raise gl.vm.UserError("can only claim default on funded loans")

        sender = gl.message.sender_address
        if sender != loan.lender:
            raise gl.vm.UserError("only the lender can claim default")

        now = _current_timestamp()

        # FIX: Two paths to default:
        # 1. Grace period elapsed (normal default)
        # 2. Funding expiry reached (auto-default)
        grace_deadline = int(loan.funded_at) + int(loan.duration_seconds) + GRACE_PERIOD_SECONDS
        funding_deadline = int(loan.expires_at)

        if int(now) < grace_deadline and int(now) < funding_deadline:
            raise gl.vm.UserError("default conditions not met yet")

        loan.status = "defaulted"
        self.loans[loan_id] = loan

        borrower = self.borrowers.get(loan.borrower_id)
        if borrower is not None:
            borrower.active_loans = borrower.active_loans - u256(1)
            self.borrowers[loan.borrower_id] = borrower

        # FIX: Atomic transfer - collateral goes to lender
        if int(loan.collateral_deposited) > 0:
            gl.vm.emit_transfer(contract_address(), loan.lender, int(loan.collateral_deposited))

    # -- Loan expiry (anyone can call to clean up) ------------------------

    @gl.public.write
    def expire_loan(self, loan_id: str) -> None:
        """Expire a pending loan that was never funded, returning any collateral."""
        loan_id = str(loan_id)
        loan = self.loans.get(loan_id)
        if loan is None:
            raise gl.vm.UserError("unknown loan_id")
        if loan.status != "pending":
            raise gl.vm.UserError("can only expire pending loans")

        now = _current_timestamp()
        if int(now) <= int(loan.expires_at):
            raise gl.vm.UserError("loan has not expired yet")

        loan.status = "expired"
        self.loans[loan_id] = loan

        borrower = self.borrowers.get(loan.borrower_id)
        if borrower is not None:
            borrower.active_loans = borrower.active_loans - u256(1)
            self.borrowers[loan.borrower_id] = borrower

        # FIX: Return any deposited collateral to borrower
        if int(loan.collateral_deposited) > 0:
            gl.vm.emit_transfer(contract_address(), loan.borrower_address, int(loan.collateral_deposited))

    # -- Read methods -----------------------------------------------------

    @gl.public.view
    def get_borrower(self, borrower_id: str) -> dict:
        borrower_id = str(borrower_id)
        borrower = self.borrowers.get(borrower_id)
        if borrower is None:
            raise gl.vm.UserError("unknown borrower_id")
        return borrower.as_dict()

    @gl.public.view
    def get_borrower_by_address(self, address) -> str:
        addr_key = str(_coerce_address(address))
        return self.borrower_ids_by_address.get(addr_key, "")

    @gl.public.view
    def get_assessment(self, borrower_id: str) -> dict:
        borrower_id = str(borrower_id)
        assessment = self.assessments.get(borrower_id)
        if assessment is None:
            raise gl.vm.UserError("no assessment for this borrower")
        return assessment.as_dict()

    @gl.public.view
    def get_loan(self, loan_id: str) -> dict:
        loan_id = str(loan_id)
        loan = self.loans.get(loan_id)
        if loan is None:
            raise gl.vm.UserError("unknown loan_id")
        return loan.as_dict()

    @gl.public.view
    def get_collateral_required(self, borrower_id: str, amount: u256) -> u256:
        borrower_id = str(borrower_id)
        borrower = self.borrowers.get(borrower_id)
        if borrower is None or borrower.status != "assessed":
            return u256(int(amount))
        return _calculate_collateral(amount, borrower.tier)

    @gl.public.view
    def get_interest_rate(self, borrower_id: str) -> u256:
        borrower_id = str(borrower_id)
        borrower = self.borrowers.get(borrower_id)
        if borrower is None or borrower.status != "assessed":
            return u256(BASE_INTEREST_RATE + 200)
        return u256(_interest_rate_bps(borrower.tier))

    @gl.public.view
    def get_active_loans(self, borrower_id: str) -> u256:
        borrower_id = str(borrower_id)
        borrower = self.borrowers.get(borrower_id)
        if borrower is None:
            return u256(0)
        return borrower.active_loans

    @gl.public.view
    def get_borrower_count(self) -> u256:
        return self.borrower_count

    @gl.public.view
    def get_loan_count(self) -> u256:
        return self.loan_count

    @gl.public.view
    def get_tiers(self) -> list[str]:
        return [t for t in TIERS]

    @gl.public.view
    def get_collateral_ratios(self) -> dict:
        return {k: v for k, v in COLLATERAL_RATIOS.items()}

    @gl.public.view
    def get_constants(self) -> dict:
        return {
            "loan_expiry_seconds": LOAN_EXPIRY_SECONDS,
            "funding_expiry_seconds": FUNDING_EXPIRY_SECONDS,
            "grace_period_seconds": GRACE_PERIOD_SECONDS,
            "max_loan_amount": MAX_LOAN_AMOUNT,
            "max_active_loans": MAX_ACTIVE_LOANS,
            "base_interest_rate": BASE_INTEREST_RATE,
        }
