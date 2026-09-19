# Hourly temperature episode backtest

This experiment predicts the **onset of a recorded temperature excursion in the next 24 hours**.
It is a temperature-risk proxy, not a confirmed equipment failure label.

## Main difference from `feat/ml-pipeline`

The two results answer different questions and their headline metrics must not be compared as if the
targets were identical:

| | This experiment | `feat/ml-pipeline` Head A |
|---|---|---|
| Target | First numeric value outside `[3,40] C` after 24/72 clean hours | L5: durable bad-state episode or future telemetry silence |
| Positive-class composition | 1,500 / 1,338 rare episode onsets | 108,425 positives, of which 108,170 (99.76%) are silence |
| Entity | Channel; channel ID is not a feature | Channel plus inferred object context |
| Object mapping | Not used until the updated catalog provides real object IDs | First tag component is treated as an object despite the expert answer |
| Same-second policy | Unordered set; min/max/mean aggregation | Event sequence ordered by timestamp without a tie breaker |
| Deduplication | Full semantic content | `DISTINCT ON (event_id)`, which drops 1,669,268 nonidentical rows in 2021-2023 |
| Evaluation | Three fixed train/validation/threshold/test folds and an unseen-channel protocol | 2026 H1 was reused after the first final run |
| Status | Retrospective evidence; no untouched-holdout claim | Published final report is stale relative to the latest training code |

Head A's reported precision `0.700` and recall `0.558` are stronger headline numbers, but mostly
measure next-day channel availability. This experiment reports lower precision and recall for a rare,
explicitly observable thermal episode. It does not claim to have beaten Head A on the same task.
Numerically, its three-fold AP `0.265` is above Head A_strict's published AP `0.213`, but that is also
only context: A_strict predicts a different bad-state/silence label on a different eligible population.

## Data and target

- 600 temperature channels from the current catalog.
- 1,363,583 semantically distinct records after excluding April-June 2021.
- 1,020,004 numeric records inside the validity guard `[-60,150] C`.
- 1,446,299 candidate channel-days from 2019-01-02 through 2026-06-30.
- Prediction time is local midnight in `Europe/Moscow`.
- Features use only records with timestamps before prediction time.
- The target uses `[as_of, as_of + 24h)` and is evaluated only when future telemetry reaches at
  least 25% of the channel's preceding seven-day daily cadence.
- Existing excursions are excluded using either a 24-hour or 72-hour clean lookback.
- April-June 2021 is removed before feature construction, following the organizer's migration note.

Same-second values are aggregated as an unordered set. The pipeline never invents a transition order
where the source has none.

## Features and models

Windows: 6, 24, 72, 168 and 720 hours.

Features include first/last/min/max/mean/std, range, change, slope, sampling density, numeric fraction,
time since the last numeric value, distance to the inferred bounds and a 30-day median/IQR baseline.
Channel ID and inferred object ID are not model inputs.

Each fold compares:

1. balanced logistic regression;
2. histogram gradient boosting;
3. a fixed rule based on boundary distance and 24-hour trend.

The model family is selected on the first half-year, the alert threshold on the second half-year, and
the next half-year is used as the retrospective test. Test labels never select the model or threshold.

## Main results

### 24-hour clean lookback

| Test | Precision | Recall | AP | AP lift | False alerts / 100 channel-days |
|---|---:|---:|---:|---:|---:|
| 2024 H1 | 0.339 | 0.496 | 0.235 | 23.68x | 0.959 |
| 2025 H1 | 0.275 | 0.402 | 0.246 | 29.70x | 0.877 |
| 2026 H1 | 0.449 | 0.421 | 0.332 | 31.88x | 0.536 |
| **Mean** | **0.354** | **0.439** | **0.271** | **28.42x** | **0.791** |

The earlier daily temperature baseline had AP `0.117`, precision `0.185` and recall `0.058` on one
previously inspected 2026 period. The new AP is 2.32 times higher, with much higher recall, but this is
not a one-feature ablation: the target, coverage rule and evaluation protocol were all improved.

Gradient boosting won all three temporal validation periods. Its validation AP was
`0.205/0.276/0.245`, compared with `0.144/0.145/0.118` for the fixed boundary/trend rule. The model is
learning more than simple proximity to `3 C`, although the latest 24-hour value is the dominant feature.

### 72-hour clean lookback

| Test | Precision | Recall | AP | AP lift | False alerts / 100 channel-days |
|---|---:|---:|---:|---:|---:|
| 2024 H1 | 0.309 | 0.447 | 0.205 | 24.89x | 0.823 |
| 2025 H1 | 0.273 | 0.386 | 0.251 | 32.08x | 0.803 |
| 2026 H1 | 0.427 | 0.373 | 0.339 | 36.94x | 0.459 |
| **Mean** | **0.336** | **0.402** | **0.265** | **31.30x** | **0.695** |

The 72-hour definition produces slightly lower recall and fewer false alerts. It is the safer default
because repeated bad days are less likely to be counted as separate onsets.

### Channels unseen during training

Twenty percent of channels are selected by a deterministic hash. They are removed from training and
used only for validation, threshold selection and testing.

- 24-hour target mean: precision `0.257`, recall `0.335`, AP `0.230`.
- 72-hour target mean: precision `0.542`, recall `0.340`, AP `0.347`.

The 72-hour unseen-channel result is promising but fragile: the three test folds contain only 16, 23
and 22 positives. The 2026 precision of `1.0` represents eight alerts and must not be presented as a
stable production estimate.

## Target audit and limitations

- 3,415 of 3,594 raw 72-hour-separated excursion onsets (95.0%) contain only the exact values
  `0`, `1` or `2 C` at onset.
- No out-of-range record has `alarm=true`.
- The 1,338 eligible 72-hour episode days span 292 channels; the ten most frequent channels account
  for 14.8%, so the label is not dominated by a few IDs.
- 1,621 of 27,546 bad seconds also contain an in-range value at the same second. Aggregating the set
  avoids arbitrary ordering but cannot explain the physical meaning.
- The `[3,40] C` threshold is inferred from recorded sensor text and is not owner-confirmed.

The correct product wording is therefore **forecast of a temperature excursion**, not sensor-failure
prediction. Linking it to preventive maintenance still requires owner confirmation of the threshold
and the updated channel-to-object catalog.

## Reproduction

```powershell
$env:PYTHONPATH='backend'
python -m ml.temperature_hourly `
  --database C:/path/to/normalized.duckdb `
  --catalog C:/path/to/справочник_каналов_датчиков.csv `
  --output C:/path/to/temperature_hourly_v1

python -m ml.rolling_backtest `
  --features C:/path/to/temperature_hourly_v1/temperature_episode_features.parquet `
  --output C:/path/to/temperature_hourly_backtest_72h `
  --clean-hours 72
```

Raw events, feature Parquet, row-level predictions and trained binaries are intentionally excluded from
Git. Aggregated reports and source hashes are committed beside this document.
