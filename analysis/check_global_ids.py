import pathlib,sys,json
OUT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(OUT/'deps'))
import duckdb
con=duckdb.connect(str(OUT/'audit.duckdb'))
con.execute("SET memory_limit='6GB'")
con.execute('SET threads=2')
con.execute('SET preserve_insertion_order=false')
totals={}
for shard in range(16):
 q=con.execute(f'''SELECT count(*) unique_ids,sum(n) valid_id_rows,sum(n-1) excess_rows,
  count(*) FILTER(WHERE first_year<>last_year) ids_in_multiple_years FROM
  (SELECT id,count(*) n,min(year(event_date)) first_year,max(year(event_date)) last_year
   FROM all_events WHERE id IS NOT NULL AND id%16={shard} GROUP BY id)''')
 data=dict(zip([x[0] for x in q.description],q.fetchone()))
 for k,v in data.items(): totals[k]=totals.get(k,0)+v
 print(f'Global ID partition {shard+1}/16',flush=True)
res=[totals]
p=OUT/'final_checks.json'; data=json.loads(p.read_text(encoding='utf-8')); data['global_ids']=res
p.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print(res,flush=True)
con.close()
