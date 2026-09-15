"""Read every annual CSV, retain compact analytical tables and reproducible audit results."""
import sys, pathlib, json, time, zipfile, zlib
sys.path.insert(0, str(pathlib.Path(__file__).parent / 'deps'))
import duckdb
from audit_paths import source_sql

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / 'analysis'
con = duckdb.connect(str(OUT / 'audit.duckdb'))
con.execute("SET memory_limit='6GB'")
con.execute("SET threads=4")
con.execute("SET temp_directory='analysis/duckdb_tmp'")

def records(sql):
    q=con.execute(sql)
    names=[x[0] for x in q.description]
    return [dict(zip(names,row)) for row in q.fetchall()]
def save(name, obj):
    (OUT/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

con.execute(f"CREATE OR REPLACE TABLE channels AS SELECT * FROM read_csv('{source_sql('справочник_каналов_датчиков.csv')}')")
con.execute(f"CREATE OR REPLACE TABLE objects AS SELECT * FROM read_csv('{source_sql('справочник_объектов_диспетчер.csv')}')")
manifest=json.loads((OUT/'archive_manifest.json').read_text(encoding='utf-8'))
profiles=[]
for item in manifest:
    year=item['archive'][12:16]
    p=OUT/'extracted'/item['files'][0]['name']
    if not p.exists() or p.stat().st_size != item['files'][0]['size']:
        raise ValueError(f'Missing or incomplete CSV: {p}. Run extract_archives.py first.')
    start=time.time()
    table='events_'+year
    print('Loading '+year,flush=True)
    con.execute(f'''CREATE OR REPLACE TABLE {table} AS SELECT
        try_cast("ид_события" AS BIGINT) id, try_cast("ид_канала_данных" AS BIGINT) channel,
        try_cast("дата" AS DATE) event_date, try_cast("дата" || ' ' || "время" AS TIMESTAMP) ts,
        "тревожное" alarm_raw, try_cast("тревожное" AS BOOLEAN) alarm,
        "значение_датчика" sensor_value
        FROM read_csv('{p.as_posix()}', all_varchar=true, strict_mode=true, ignore_errors=false)''')
    profile={'year':year,'file_bytes':p.stat().st_size}
    profile['summary']=records(f'''SELECT count(*) row_count, min(ts) min_ts, max(ts) max_ts,
        min(id) min_id, max(id) max_id, count(DISTINCT id) unique_ids,
        count(DISTINCT channel) channels, count(DISTINCT event_date) active_days,
        count(*) FILTER (WHERE alarm) alarms,
        count(*) FILTER (WHERE id IS NULL OR channel IS NULL OR ts IS NULL OR alarm IS NULL OR sensor_value IS NULL) null_or_invalid_rows,
        count(*) FILTER (WHERE c."ид_канала_данных" IS NULL) unknown_channel_rows,
        count(DISTINCT e.channel) FILTER (WHERE c."ид_канала_данных" IS NULL) unknown_channels,
        count(*) FILTER (WHERE year(event_date)<>{year}) wrong_year_rows
        FROM {table} e LEFT JOIN channels c ON e.channel=c."ид_канала_данных"''')[0]
    profile['alarm_raw']=records(f'SELECT alarm_raw,count(*) n FROM {table} GROUP BY 1')
    profile['active_days']=records(f'SELECT event_date,count(*) n,count(*) FILTER (WHERE alarm) alarms,count(DISTINCT channel) channels FROM {table} GROUP BY 1 ORDER BY 1')
    con.execute(f'''CREATE OR REPLACE TABLE channels_{year} AS SELECT channel,count(*) n,
        min(ts) first_ts,max(ts) last_ts,count(*) FILTER (WHERE alarm) alarms,
        count(DISTINCT event_date) active_days FROM {table} GROUP BY 1''')
    profile['top_channels']=records(f'SELECT * FROM channels_{year} ORDER BY n DESC LIMIT 15')
    con.execute(f'''CREATE OR REPLACE TABLE values_{year} AS SELECT channel,alarm,sensor_value,count(*) n FROM {table} GROUP BY 1,2,3''')
    profile['types']=records(f'''SELECT c."тип_датчика" sensor_type,sum(v.n) n,
        sum(v.n) FILTER (WHERE alarm) alarms,count(DISTINCT channel) channels,
        sum(v.n) FILTER (WHERE try_cast(sensor_value AS DOUBLE) IS NOT NULL) numeric_rows,
        min(try_cast(sensor_value AS DOUBLE)) min_numeric,max(try_cast(sensor_value AS DOUBLE)) max_numeric
        FROM values_{year} v LEFT JOIN channels c ON channel=c."ид_канала_данных" GROUP BY 1 ORDER BY n DESC''')
    profile['text_values']=records(f'''SELECT c."тип_датчика" sensor_type,alarm,sensor_value,sum(n) n
        FROM values_{year} v LEFT JOIN channels c ON channel=c."ид_канала_данных"
        WHERE try_cast(sensor_value AS DOUBLE) IS NULL GROUP BY 1,2,3 ORDER BY 1,2,4 DESC''')
    profile['elapsed_seconds']=round(time.time()-start,1)
    profiles.append(profile)
    save('annual_profiles.json',profiles)
    print(json.dumps({'year':year,**profile['summary'],'seconds':profile['elapsed_seconds']},ensure_ascii=False,default=str),flush=True)

con.execute('CREATE OR REPLACE VIEW all_events AS '+ ' UNION ALL '.join('SELECT * FROM events_'+p['year'] for p in profiles))
con.execute('CREATE OR REPLACE VIEW all_values AS '+ ' UNION ALL '.join('SELECT * FROM values_'+p['year'] for p in profiles))
save('global_types.json',records('''SELECT c."тип_датчика" sensor_type,sum(n) n,sum(n) FILTER (WHERE alarm) alarms,count(DISTINCT channel) channels FROM all_values v LEFT JOIN channels c ON channel=c."ид_канала_данных" GROUP BY 1 ORDER BY n DESC'''))
save('global_values.json',records('''SELECT c."тип_датчика" sensor_type,alarm,sensor_value,sum(n) n FROM all_values v LEFT JOIN channels c ON channel=c."ид_канала_данных" GROUP BY 1,2,3 ORDER BY 1,4 DESC'''))
print('All annual profiles complete',flush=True)
con.close()
