/**
 * Мелкие общие элементы экранов. Живое.
 */
import type { Schemas } from "../api/client";
import { title } from "../vocab";

type Source = Schemas["ForecastItem"]["source"];

/**
 * Метка происхождения записи. Расчёт сервиса (live) не помечаем; эмуляцию и
 * заглушку каркаса помечаем всегда, чтобы синтетика не выглядела настоящей.
 */
export function SourceBadge({ source }: { source: Source }) {
  if (source === "live") return null;
  return <span className={`badge badge--${source}`}>{title("source", source)}</span>;
}

interface PagerProps {
  page: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
}

export function Pager({ page, pageSize, total, onPage }: PagerProps) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total === 0) return null;
  const first = (page - 1) * pageSize + 1;
  const last = Math.min(total, page * pageSize);
  return (
    <div className="pager">
      <button type="button" className="button" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Назад
      </button>
      <span>
        {first}–{last} из {total} · страница {page} из {pages}
      </span>
      <button type="button" className="button" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        Вперёд
      </button>
    </div>
  );
}
