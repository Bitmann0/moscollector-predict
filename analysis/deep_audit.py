import pathlib,sys,json,datetime,time
ROOT=pathlib.Path(__file__).resolve().parent.parent
OUT=ROOT/'analysis'
sys.path.insert(0,str(OUT/'deps'))
import duckdb
from audit_paths import source_sql
con=duckdb.connect(str(OUT/'audit.duckdb'))
con.execute("SET memory_limit='6GB'")
con.execute('SET threads=4')
def rows(sql):
    q=con.execute(sql)
    return [dict(zip([x[0] for x in q.description],r)) for r in q.fetchall()]
result={}
def checkpoint():
    (OUT/'deep_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

profiles=json.loads((OUT/'annual_profiles.json').read_text(encoding='utf-8'))
result['missing_dates']={}
result['daily_extremes']={}
result['duplicates']={}
for p in profiles:
    year=p['year']; summary=p['summary']
    lo=datetime.date.fromisoformat(summary['min_ts'][:10]); hi=datetime.date.fromisoformat(summary['max_ts'][:10])
    dates={x['event_date'] for x in p['active_days']}
    result['missing_dates'][year]=[str(lo+datetime.timedelta(days=i)) for i in range((hi-lo).days+1) if str(lo+datetime.timedelta(days=i)) not in dates]
    result['daily_extremes'][year]={'lowest':sorted(p['active_days'],key=lambda x:x['n'])[:5],'highest':sorted(p['active_days'],key=lambda x:x['n'],reverse=True)[:5]}
    if summary['row_count']!=summary['unique_ids']:
        table='events_'+year
        con.execute(f'CREATE OR REPLACE TEMP TABLE dupids AS SELECT id,count(*) n FROM {table} GROUP BY id HAVING count(*)>1')
        result['duplicates'][year]=rows(f'''SELECT count(*) duplicate_ids,sum(n-1) excess_rows,
          count(*) FILTER (WHERE variants>1) conflicting_ids,sum(n-variants) exact_duplicate_excess,max(n) max_multiplicity
          FROM (SELECT e.id,count(*) n,count(DISTINCT (channel,ts,alarm_raw,sensor_value)) variants
          FROM {table} e JOIN dupids USING(id) GROUP BY 1)''')[0]
        result['duplicates'][year]['examples']=rows(f'SELECT e.* FROM {table} e JOIN (SELECT id FROM dupids ORDER BY n DESC LIMIT 3) USING(id) ORDER BY id,ts LIMIT 12')
    checkpoint()
    print('Duplicates checked '+year,flush=True)
result['invalid_rows']=rows('SELECT * FROM events_2025 WHERE id IS NULL OR channel IS NULL OR ts IS NULL OR alarm IS NULL OR sensor_value IS NULL')
result['unique_channels']=rows('SELECT count(DISTINCT channel) n FROM all_values')
result['unused_catalog_channels']=rows('SELECT c.* FROM channels c ANTI JOIN (SELECT DISTINCT channel FROM all_values) v ON c."ид_канала_данных"=v.channel')
result['top_text_signals']=rows('''SELECT c."тип_датчика" sensor_type,sensor_value,alarm,sum(n) n,count(DISTINCT channel) channels
  FROM all_values v LEFT JOIN channels c ON channel=c."ид_канала_данных"
  WHERE try_cast(sensor_value AS DOUBLE) IS NULL GROUP BY 1,2,3 ORDER BY n DESC LIMIT 100''')
result['numeric_extremes']=rows('''SELECT c."тип_датчика" sensor_type,min(try_cast(sensor_value AS DOUBLE)) min_value,
  max(try_cast(sensor_value AS DOUBLE)) max_value,sum(n) n FROM all_values v JOIN channels c ON channel=c."ид_канала_данных"
  WHERE try_cast(sensor_value AS DOUBLE) IS NOT NULL GROUP BY 1''')
result['bad_numeric_examples']=rows('''SELECT c."тип_датчика" sensor_type,channel,sensor_value,sum(n) n FROM all_values v JOIN channels c ON channel=c."ид_канала_данных"
 WHERE try_cast(sensor_value AS DOUBLE)<0 OR try_cast(sensor_value AS DOUBLE)>100 GROUP BY 1,2,3 ORDER BY n DESC LIMIT 25''')
con.execute(f'''CREATE OR REPLACE TEMP TABLE sample AS SELECT "ид_события" id,"ид_канала_данных" channel,
  try_cast("дата"||' '||"время" AS TIMESTAMP) ts,"тревожное" alarm,"значение_датчика" sensor_value
  FROM read_csv('{source_sql('журнал_событий_пример.csv')}', types={{'время':'VARCHAR','дата':'VARCHAR'}})''')
result['sample_overlap']=rows('''SELECT count(*) matching_pairs,count(DISTINCT s.id) sample_ids,
 count(*) FILTER (WHERE s.ts=e.ts) same_ts,count(*) FILTER (WHERE s.channel<>e.channel OR s.sensor_value IS DISTINCT FROM e.sensor_value OR s.alarm<>e.alarm) conflicting_payload,
 min(e.ts) earliest_original,max(e.ts) latest_original FROM sample s JOIN all_events e USING(id)''')
result['sample_overlap_examples']=rows('SELECT s.id,s.ts sample_ts,e.ts archive_ts,s.channel FROM sample s JOIN all_events e USING(id) ORDER BY s.id LIMIT 6')
checkpoint()
print('Semantics and sample overlap checked',flush=True)
result['prefix_join']=rows('''SELECT count(*) channels,count(DISTINCT split_part("тег_инженерной_системы",'-',1)) prefixes,
 count(*) FILTER (WHERE o."ид_объект" IS NOT NULL) naive_prefix_matching_rows
 FROM channels c LEFT JOIN objects o ON try_cast(split_part(c."тег_инженерной_системы",'-',1) AS BIGINT)=o."ид_объект"''')
result['hourly_peak']=rows('''SELECT date_trunc('hour',ts) hour_start,count(*) n FROM all_events GROUP BY 1 ORDER BY n DESC LIMIT 10''')
# State changes are an exploratory proxy, not confirmed real-world incident labels.
con.execute('''CREATE OR REPLACE TABLE operational_states AS SELECT DISTINCT e.channel,e.ts,e.sensor_value,e.alarm,c."тип_датчика" sensor_type
 FROM all_events e JOIN channels c ON e.channel=c."ид_канала_данных"
 WHERE c."тип_датчика" IN ('Состояние насоса','Датчик затопления','Датчик дыма','Состояние вентилятора')''')
result['state_onsets']=rows('''WITH ordered AS (SELECT *,lag(sensor_value) OVER(PARTITION BY channel ORDER BY ts,sensor_value,alarm) previous_value,
 lag(ts) OVER(PARTITION BY channel ORDER BY ts,sensor_value,alarm) previous_ts FROM operational_states)
 SELECT year(ts) yr,sensor_type,sensor_value,count(*) n,count(*) FILTER (WHERE sensor_value IS DISTINCT FROM previous_value) transitions,
 count(DISTINCT channel) channels,count(*) FILTER (WHERE previous_ts IS NULL OR ts-previous_ts>INTERVAL '7 days') after_long_gap
 FROM ordered WHERE sensor_value IN ('Затоплен','Обнаружен дым','Неисправен') GROUP BY 1,2,3 ORDER BY 1,2,3''')
result['simultaneous_conflicts_operational']=rows('''SELECT sensor_type,count(*) conflicting_channel_seconds FROM
 (SELECT sensor_type,channel,ts,count(DISTINCT sensor_value) n FROM operational_states GROUP BY 1,2,3 HAVING n>1) GROUP BY 1''')
checkpoint()
print('Deep audit complete',flush=True)
con.close()
