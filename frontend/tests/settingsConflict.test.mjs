import assert from "node:assert/strict";
import { test } from "node:test";

import { changedPaths, reconcile } from "../src/settingsConflict.ts";

test("разные поля двух администраторов сохраняются вместе", () => {
  const original = { gas: { alarm_pct: 1, critical_pct: 5 }, notify: { classes: ["alarm"] } };
  const mine = { gas: { alarm_pct: 2, critical_pct: 5 }, notify: { classes: ["alarm"] } };
  const current = { gas: { alarm_pct: 1, critical_pct: 4 }, notify: { classes: ["critical"] } };
  const result = reconcile(original, mine, current);
  assert.deepEqual(result, {
    values: { gas: { alarm_pct: 2, critical_pct: 4 }, notify: { classes: ["critical"] } },
    conflicts: [],
  });
  assert.deepEqual(current, { gas: { alarm_pct: 1, critical_pct: 4 }, notify: { classes: ["critical"] } });
});

test("одно поле с разными значениями требует решения", () => {
  const result = reconcile({ gas: { alarm_pct: 1 } }, { gas: { alarm_pct: 2 } }, { gas: { alarm_pct: 3 } });
  assert.deepEqual(result, { values: { gas: { alarm_pct: 2 } }, conflicts: ["gas.alarm_pct"] });
});

test("одинаковые правки не конфликтуют", () => {
  const result = reconcile({ gas: { alarm_pct: 1 } }, { gas: { alarm_pct: 2 } }, { gas: { alarm_pct: 2 } });
  assert.deepEqual(result, { values: { gas: { alarm_pct: 2 } }, conflicts: [] });
});

test("списки дней и классов считаются целыми полями", () => {
  const original = { window: { days: [0, 1] }, notify: { classes: ["alarm"] } };
  assert.deepEqual(changedPaths(original, { window: { days: [0, 2] }, notify: { classes: ["critical"] } }), ["window.days", "notify.classes"]);
  assert.deepEqual(reconcile(original,
    { window: { days: [0, 2] }, notify: { classes: ["alarm"] } },
    { window: { days: [1, 2] }, notify: { classes: ["critical"] } }).conflicts, ["window.days"]);
});
