# Telemetry availability sensitivity study

This experiment repairs and isolates the strongest signal from `feat/ml-pipeline`: predicting that a
channel will have no telemetry on the next globally covered calendar day.

It does **not** call silence a physical sensor failure. The positive class is an availability event,
confirmed offline by a later return of telemetry. Long disappearances and the end of history are
censored as unknown.

## Why two definitions

The published Head A combines durable bad-state episodes with a permissive silence target. Its strong
headline result is 99.76% silence, so the useful scientific question is how performance changes when
the availability event is made stricter.

| Definition | Past activity | Required recovery | Rows | Positive rate |
|---|---:|---:|---:|---:|
| Broad availability warning | at least 7 of previous 30 days | within 30 days | 2,483,963 | 25.38% |
| Strict temporary outage | at least 25 of previous 30 days | within 7 days | 821,158 | 4.27% |

Both variants require the target day to have global telemetry coverage of at least 50% of its trailing
30-day median. The organizer-confirmed migration period and the following 30-day feature-recovery
window are excluded.

## What was corrected relative to `feat/ml-pipeline`

- Full semantic deduplication is inherited from the normalized warehouse; rows are not collapsed by
  reused `event_id`.
- Past activity is measured at prediction time. Future recovery confirms the offline label but is not
  a model feature.
- Long outages and decommissioned channels are unknown instead of convenient positives or negatives.
- Global export outages are censored.
- Channel ID and inferred tag-prefix object ID are not features.
- Three fixed model-selection, threshold-selection and test periods are reported.
- A deterministic 20% channel holdout measures transfer to channels absent from training.
- The 2026 period is explicitly retrospective because it has already been inspected.

## Results

### Broad availability warning

| Test | Precision | Recall | AP | Base rate | False alerts / 100 channel-days |
|---|---:|---:|---:|---:|---:|
| 2024 H1 | 0.697 | 0.316 | 0.617 | 0.244 | 3.355 |
| 2025 H1 | 0.722 | 0.432 | 0.682 | 0.261 | 4.345 |
| 2026 H1 | 0.685 | 0.515 | 0.705 | 0.309 | 7.314 |
| **Mean** | **0.701** | **0.421** | **0.668** | — | **5.004** |

Channels absent from model training remain similar: mean precision `0.708`, recall `0.400` and AP
`0.667`. This supports a real, transferable availability signal rather than channel-ID memorization.

The result closely reproduces the meaning of Head A's `0.700 / 0.558`, whose test base rate is 26.3%.
The remaining recall difference is expected because this pipeline censors global gaps, long outages
and missing future confirmation, and evaluates three periods instead of optimizing against one reused
final period.

### Strict temporary outage

| Test | Precision | Recall | AP | AP lift | False alerts / 100 channel-days |
|---|---:|---:|---:|---:|---:|
| 2024 H1 | 0.227 | 0.262 | 0.169 | 4.43x | 3.397 |
| 2025 H1 | 0.333 | 0.227 | 0.239 | 5.59x | 1.943 |
| 2026 H1 | 0.284 | 0.181 | 0.178 | 5.90x | 1.371 |
| **Mean** | **0.281** | **0.223** | **0.195** | **5.31x** | **2.237** |

The unseen-channel mean is precision `0.274`, recall `0.297` and AP `0.215`. This is close to
`feat/ml-pipeline` Head A_strict's published AP `0.213`, providing an independent explanation for why
its strict head did not meet the headline precision/recall target.

## Operational interpretation

The broad metric is valid, but the selected threshold produces too many channel-level alerts for work
orders:

- 2024 H1: 14,971 alerts over 144 evaluated days, about 104 per day.
- 2025 H1: 22,998 over 157 days, about 146 per day.
- 2026 H1: 38,925 over 139 days, about 280 per day.

Limiting output to the top one or three channels per day keeps precision around `0.74-0.79`, but recall
falls below 1.1%. Therefore the broad head is suitable for a telemetry-health dashboard, background
quality monitoring and object-level aggregation. It is not suitable for creating one maintenance
request per channel alert.

The strict head is closer to an actionable incident but does not achieve acceptable precision or
recall. A real object mapping is needed to collapse correlated channel disappearances into one outage
case before evaluating a dispatcher-facing alert budget.

## Recommended product claim

> The system forecasts next-day telemetry availability with approximately 70% precision across three
> retrospective temporal tests. This metric concerns data availability, not confirmed physical
> equipment failures. Physical anomaly heads are evaluated separately.

This wording makes the metric reproducible and useful without presenting a common reporting pattern as
evidence of equipment failure.

## Reproduction

```powershell
$env:PYTHONPATH='backend'

python -m ml.availability_prepare `
  --daily-features C:/path/to/daily_features.parquet `
  --output C:/path/to/availability_broad `
  --min-active-days 7 `
  --recovery-days 30

python -m ml.availability_backtest `
  --samples C:/path/to/availability_broad/availability_samples.parquet `
  --output C:/path/to/availability_broad_backtest
```

The feature Parquet and row-level samples stay outside Git. Source hashes, aggregate manifests and both
reports are committed beside this document.
