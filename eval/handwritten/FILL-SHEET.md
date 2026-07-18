# Handwriting Fill Sheet — write EXACTLY these values, all fake

Rules: use the pen, natural handwriting (don't draw block letters unless that's how you write). Write each value in its labeled box. If you make a typo, cross it out and rewrite beside it (realistic!) — but tell Claude which value ended up final. Save each file as a NEW png/jpg keeping the same base name (e.g. w2_hand_01_filled.png). Do NOT write any real names, SSNs, or amounts.

## w2_hand_01.png (Form W-2, Copy 1)

| Box | Write this |
|---|---|
| a — Employee's SSN | 437-88-2210 |
| b — EIN | 47-3391208 |
| c — Employer name/address | Hollow Pine Outfitters / 802 Timberline Rd / Ogden, UT 84401 |
| e — Employee name | Priya N. Vasquez |
| Employee address | 1194 Fox Run Ave, Layton, UT 84041 |
| 1 — Wages | 54,318.77 |
| 2 — Federal income tax withheld | 6,204.15 |

## 1099nec_hand_01.png (Form 1099-NEC, Copy 1)

| Box | Write this |
|---|---|
| PAYER name/address | Redrock Media Group / 55 Canyon View Dr / St. George, UT 84770 |
| PAYER TIN | 82-4415906 |
| RECIPIENT TIN | 521-64-8837 |
| RECIPIENT name | Omar T. Lindqvist |
| Recipient address | 3308 Juniper Ct, Cedar City, UT 84720 |
| 1 — Nonemployee compensation | 19,847.02 |

## 1098_hand_01.png (Form 1098, Copy B)

| Box | Write this |
|---|---|
| RECIPIENT'S/LENDER'S name | Wasatch Home Lending |
| Lender address | 240 S Main St, Bountiful, UT 84010 |
| PAYER'S/BORROWER'S name | Celeste M. Okonkwo |
| Borrower address | 77 Alpine Meadow Ln, Draper, UT 84020 |
| 1 — Mortgage interest received | 11,932.60 |

When done: zip the three filled files, send back, drop them in `eval/handwritten/` (or tell Claude where they are). Claude imports them into the test set, visually verifies every field against this sheet, merges labels, and reruns the eval with a new `hand` bucket.
