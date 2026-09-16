# TrustLend

Under-collateralized Lending powered by on-chain reputation.

## Deployed Contract

- **Address:** `0xF06e91F11BD6D2592cf8EbB7CC4780c83346fAfc`
- **Explorer:** https://explorer-studio.genlayer.com/address/0xF06e91F11BD6D2592cf8EbB7CC4780c83346fAfc

## Overview

TrustLend enables lending with less collateral by linking real-world identity to on-chain reputation. Borrowers register evidence of their professional standing, financial history, and social proof. The network's AI validators assess creditworthiness through consensus, producing a reputation tier that determines collateral requirements and interest rates.

## How It Works

1. **Register** - Borrowers register with evidence URLs (LinkedIn, GitHub, portfolio, references)
2. **Assess** - AI consensus evaluates creditworthiness across 4 dimensions
3. **Borrow** - Collateral requirements adjust automatically based on reputation tier
4. **Repay** - Borrowers repay with tier-based interest rates

## Reputation Tiers

| Tier | Score | Collateral Required | Interest Rate |
|------|-------|---------------------|---------------|
| Platinum | 80+ | 10% | 1% |
| Gold | 65+ | 25% | 3% |
| Silver | 50+ | 50% | 4% |
| Bronze | 35+ | 75% | 5% |
| None | <35 | 100% | 7% |

## Assessment Dimensions

- **Financial History** (30%) - Track record of financial responsibility
- **Professional Standing** (25%) - Reputation in professional community
- **Project Viability** (25%) - Quality of the project being financed
- **Social Proof** (20%) - Endorsements and references

## Contract Methods

### Write Methods

| Method | Description |
|--------|-------------|
| `register_borrower(evidence_urls)` | Register with evidence for assessment |
| `update_evidence(borrower_id, evidence_urls)` | Update evidence before assessment |
| `assess_borrower(borrower_id)` | Run AI credit assessment |
| `create_loan(amount)` | Create a loan request |
| `deposit_collateral(loan_id)` | Deposit collateral (payable) |
| `fund_loan(loan_id)` | Fund a pending loan |
| `repay_loan(loan_id)` | Repay a funded loan (payable) |
| `claim_default(loan_id)` | Claim default after grace period |

### View Methods

| Method | Description |
|--------|-------------|
| `get_borrower(borrower_id)` | Get borrower profile |
| `get_borrower_by_address(address)` | Get borrower ID by address |
| `get_assessment(borrower_id)` | Get latest assessment |
| `get_loan(loan_id)` | Get loan details |
| `get_collateral_required(borrower_id, amount)` | Calculate required collateral |
| `get_interest_rate(borrower_id)` | Get current interest rate |
| `get_active_loans(borrower_id)` | Get active loan count |
| `get_borrower_count()` | Total registered borrowers |
| `get_loan_count()` | Total loans created |

## Running Tests

### Direct VM Tests (fast, no network)

```bash
cd trust-lend
pytest tests/direct/ -v
```

### Integration Tests (requires GenLayer network)

```bash
cd trust-lend
gltest tests/integration/test_trust_lend.py -v
```

### Final Deployment Test (deploy + test all methods)

```bash
cd trust-lend
gltest tests/integration/test_final_deploy.py -v -s
```

## Deployment

Deploy via GenLayer Studio: https://studio.genlayer.com

Or via CLI:
```bash
gltest tests/integration/test_final_deploy.py -v -s
```

## Architecture

```
trust-lend/
├── contracts/
│   └── trust_lend.py          # Main intelligent contract
├── tests/
│   ├── direct/
│   │   ├── _helpers.py         # Shared test utilities
│   │   ├── test_helpers.py     # Unit tests for helpers
│   │   └── test_trust_lend.py  # Direct VM tests
│   └── integration/
│       ├── test_trust_lend.py       # Integration tests
│       └── test_final_deploy.py     # Deploy + test all methods
├── deployment.toml
├── gltest.config.yaml
├── requirements.txt
└── README.md
```

## Trust Model

- Evidence is public and validator-checked
- Stored scores are medians of recent assessments (robust to gaming)
- Red flags cap tier at bronze
- Collateral ratios are deterministic arithmetic on tier
- Grace period protects borrowers from immediate default claims

## License

MIT
