/** Слияние настроек после 409. Массивы — одно поле, объекты — набор полей. */
export const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);

export function getAt(values: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((node, key) => (node as Record<string, unknown> | undefined)?.[key], values);
}

export function setAt<T>(values: T, path: string, value: unknown): T {
  const next = structuredClone(values);
  const keys = path.split(".");
  const last = keys.pop() as string;
  const parent = keys.reduce<Record<string, unknown>>((node, key) => node[key] as Record<string, unknown>, next as Record<string, unknown>);
  parent[last] = value;
  return next;
}

export function changedPaths(before: unknown, after: unknown, prefix = ""): string[] {
  if (same(before, after)) return [];
  if (before && after && typeof before === "object" && typeof after === "object" && !Array.isArray(before) && !Array.isArray(after)) {
    return [...new Set([...Object.keys(before), ...Object.keys(after)])]
      .flatMap((key) => changedPaths((before as Record<string, unknown>)[key], (after as Record<string, unknown>)[key], prefix ? `${prefix}.${key}` : key));
  }
  return [prefix];
}

export function reconcile<T>(original: T, draft: T, current: T): { values: T; conflicts: string[] } {
  const mine = changedPaths(original, draft);
  const theirs = new Set(changedPaths(original, current));
  return {
    values: mine.reduce((values, path) => setAt(values, path, getAt(draft, path)), current),
    conflicts: mine.filter((path) => theirs.has(path) && !same(getAt(draft, path), getAt(current, path))),
  };
}
