/**
 * ЗАГЛУШКА — владелец FE-07 (C2). Заменить: счётчики на условную схему — Leaflet
 * CRS.Simple по schema.geojson: комплексы линиями, объекты точками, подсветка
 * открытых прогнозов, клик — карточка объекта (P0); каналы по пикетам при
 * приближении (P1). Библиотеку карты выбирает FE-07.
 * Контракт: типы из src/api/schema.d.ts (FeatureCollection, GET /api/v1/schema.geojson);
 * npm run typecheck должен остаться зелёным. Подпись про координаты не убирать.
 */
import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { Loaded } from "../components/StateView";

type Collection = Schemas["FeatureCollection"];

function countBy(collection: Collection, types: string[]): number {
  return collection.features.filter((feature) => types.includes(String(feature.geometry.type))).length;
}

export function Schema() {
  const load = useLoad(() => api.GET("/api/v1/schema.geojson"), []);
  return (
    <section>
      <h1>Схема сети</h1>
      <p className="note">Условная схема. Координаты заказчиком не предоставлены.</p>
      <Loaded load={load}>
        {(collection) => {
          const note = collection.properties.note;
          return (
            <>
              <dl className="kpi">
                <div className="kpi__row">
                  <dt>Объектов (точек)</dt>
                  <dd>{countBy(collection, ["Point", "MultiPoint"])}</dd>
                </div>
                <div className="kpi__row">
                  <dt>Линий (комплексов)</dt>
                  <dd>{countBy(collection, ["LineString", "MultiLineString"])}</dd>
                </div>
                <div className="kpi__row">
                  <dt>Всего элементов схемы</dt>
                  <dd>{collection.features.length}</dd>
                </div>
              </dl>
              {typeof note === "string" && note && <p className="muted">Примечание сервиса: {note}</p>}
            </>
          );
        }}
      </Loaded>
    </section>
  );
}
