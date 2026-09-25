# Laya decision and agreement slice

This is a routing experiment on the first 376 of 1,200 A_link candidates from
the April test batch. The full risk experiment is in
[LAYA_LINK_CHALLENGER.md](LAYA_LINK_CHALLENGER.md); this file must not be read
as a full temporal quality estimate.

Laya received the same history state and answered three typed questions in one
forward pass:

- unusual gap risk;
- recommended action: remote link diagnostic, defer, or insufficient data;
- urgency: routine, soon, or urgent.

The CPU cost of the full FP16 checkpoint with three questions was too high for
the remaining candidates, so the run was stopped after 376. On this slice:

| Policy | Recommendations | Hits | Lower precision |
|---|---:|---:|---:|
| LightGBM policy on the slice | 176 | 119 | 67,6% |
| Laya reranking | 183 | 114 | 62,3% |
| Intersection of both queues | 93 | 66 | **71,0%** |
| Rank ensemble | 181 | 122 | 67,4% |

The intersection is promising as a high precision, low coverage gate, but it
is based on one partial slice. It is not enough to promote the policy. Laya's
action output selected `defer` for all 376 candidates, including all 237
recorded positives in this slice. That makes the current action question
unusable without task-specific examples or fine-tuning.

Decision: keep the intersection as a candidate for a later full temporal test;
do not connect Laya's action label to the dispatcher yet. The full FP16 model,
prompt hash, and partial output are recorded in
[`laya_link_decision_slice.json`](laya_link_decision_slice.json).
