# Multi-head ML strategy

## Decision

Do not combine telemetry silence, sensor states and numeric excursions into one training label. They
have different meanings, eligible populations and operational actions. One mixed precision/recall
number cannot describe them correctly.

Use separate model heads and combine only their presentation priority.

## Heads

### 1. Telemetry availability warning

- Question: will this channel have no telemetry tomorrow?
- Label: missing next globally covered day after at least 7 active days in the previous 30, followed
  by a return within 30 days.
- Measured mean: precision `0.701`, recall `0.421`, AP `0.668`.
- Source: [experiments/availability-risk](experiments/availability-risk/README.md) (PR #9, not
  reproducible from main). Measured at 104-280 alerts per day; this is not the product A_link metric.
- Action: background health monitoring and aggregation, not an automatic repair request.

### 2. Strict temporary outage

- Question: will a normally daily channel temporarily disappear tomorrow?
- Label: at least 25 active days in the previous 30 and return within seven days.
- Measured mean: precision `0.281`, recall `0.223`, AP `0.195`.
- Source: [experiments/availability-risk](experiments/availability-risk/README.md) (PR #9).
- Action: supporting signal only until object-level grouping improves precision.

### 3. Observable physical proxy

- Question: will a recorded physical measurement cross an explicitly documented boundary?
- Current implementation: first temperature excursion outside the inferred 3-40 C range after a
  24/72-hour clean period.
- Measured 24-hour mean: precision `0.354`, recall `0.439`, AP `0.271`
  ([report](../ml/reports/TEMPERATURE_EPISODE_HOURLY.md)).
- Action: a temperature-risk card; do not label it sensor failure without owner confirmation.

### 4. Confirmed maintenance outcome

- Question: will the object require inspection, repair or replacement?
- Status: unavailable because work orders, inspections and confirmed incidents were not provided.
- Action: keep the training and inference contract ready for later labels; do not simulate validation
  metrics for this head.

## Presentation priority

Each prediction card should retain separate fields:

- `availability_risk` and its target definition;
- `physical_proxy_risk` and the observed measurement type;
- current telemetry status;
- feature availability time and prediction horizon;
- the evidence factors used by the selected head.

A UI priority can be derived from explicit rules:

1. high: a physical-proxy alert with currently available telemetry;
2. high: an actual multi-channel telemetry outage after object grouping;
3. medium: broad predicted availability warning;
4. low: weak or isolated channel-level availability risk.

Do not average raw probabilities across heads. Their base rates and labels differ. If a single learned
triage score is later required, train it against an explicit dispatcher-action label.

## Evaluation contract

Report for every head separately:

- target definition and unknown/censoring rules;
- eligible samples, positive rate and channel coverage;
- AP and lift over prevalence;
- precision, recall and false alerts per 100 eligible channel-days;
- alerts per calendar day at the selected threshold;
- temporal folds and unseen-channel results;
- limitations on mapping the proxy to physical maintenance.

This structure preserves the useful `0.70` availability result while preventing it from being cited as
`0.70` precision for physical failures.
