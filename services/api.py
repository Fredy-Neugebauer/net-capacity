from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from common import connect_db

app=FastAPI(title='NetCapacity API',version='0.1.0')
Instrumentator().instrument(app).expose(app)
@app.get('/health')
def health():
    try:
        with connect_db() as db:
            db.execute('SELECT 1')
        return {'status':'ok'}
    except Exception as exc:
        raise HTTPException(status_code=503,detail='database unavailable') from exc

@app.get('/api/summary')
def summary():
    with connect_db() as db:
        total=db.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
        interfaces=db.execute('SELECT COUNT(*) FROM interfaces').fetchone()[0]
        rows=db.execute("""SELECT d.technology, COUNT(*), ROUND(AVG(latest.utilization_percent)::numeric,1)
          FROM (SELECT DISTINCT ON (device_id,ifindex) device_id,ifindex,utilization_percent FROM metrics ORDER BY device_id,ifindex,observed_at DESC,id DESC) latest
          JOIN devices d ON d.id=latest.device_id GROUP BY d.technology ORDER BY d.technology""").fetchall()
        high=db.execute("""SELECT COUNT(*) FROM (SELECT DISTINCT ON(device_id,ifindex) utilization_percent FROM metrics ORDER BY device_id,ifindex,observed_at DESC,id DESC) x WHERE utilization_percent >= 80""").fetchone()[0]
    return {'devices':total,'interfaces':interfaces,'interfaces_over_80':high,'by_technology':[{'technology':r[0],'interfaces':r[1],'average_utilization':float(r[2])} for r in rows]}

@app.get('/api/interfaces')
def interfaces():
    with connect_db() as db:
        rows=db.execute("""SELECT d.id,d.vendor,d.technology,i.ifindex,i.name,i.capacity_bps,m.rx_bps,m.tx_bps,m.utilization_percent,m.observed_at,m.source
          FROM interfaces i JOIN devices d ON d.id=i.device_id
          LEFT JOIN LATERAL (SELECT * FROM metrics m WHERE m.device_id=i.device_id AND m.ifindex=i.ifindex ORDER BY observed_at DESC,id DESC LIMIT 1) m ON TRUE
          ORDER BY m.utilization_percent DESC NULLS LAST,d.id,i.ifindex""").fetchall()
    return [dict(zip(('device_id','vendor','technology','ifindex','name','capacity_bps','rx_bps','tx_bps','utilization_percent','observed_at','source'),r)) for r in rows]

@app.get('/api/alerts')
def alerts(limit:int=30):
    with connect_db() as db:
        rows=db.execute('SELECT device_id,ifindex,observed_at,utilization_percent,message FROM alerts ORDER BY id DESC LIMIT %s',(min(100,max(1,limit)),)).fetchall()
    return [dict(zip(('device_id','ifindex','observed_at','utilization_percent','message'),r)) for r in rows]

@app.get('/api/lags')
def lags():
    """Capacity estimate assumes all configured members are forwarding and share load."""
    with connect_db() as db:
        rows = db.execute('''
          SELECT g.device_id,g.lag_id,g.description,
            COUNT(i.ifindex) AS members,
            SUM(i.capacity_bps) AS nominal_bps,
            MAX(i.capacity_bps) AS largest_member_bps,
            SUM(COALESCE(m.rx_bps,0)) AS rx_bps,
            SUM(COALESCE(m.tx_bps,0)) AS tx_bps,
            COUNT(m.id) AS sampled_members
          FROM lag_groups g
          JOIN lag_members lm ON lm.device_id=g.device_id AND lm.lag_id=g.lag_id
          JOIN interfaces i ON i.device_id=lm.device_id AND i.ifindex=lm.ifindex
          LEFT JOIN LATERAL (
            SELECT id,rx_bps,tx_bps FROM metrics
            WHERE device_id=i.device_id AND ifindex=i.ifindex
            ORDER BY observed_at DESC,id DESC LIMIT 1
          ) m ON TRUE
          GROUP BY g.device_id,g.lag_id,g.description
          ORDER BY g.device_id,g.lag_id
        ''').fetchall()
    result=[]
    for device, lag, description, members, nominal, largest, rx, tx, sampled in rows:
        traffic=max(float(rx),float(tx))
        n1_capacity=int(nominal-largest) if members>=2 else 0
        complete=sampled==members
        result.append({'device_id':device,'lag_id':lag,'description':description,
            'members':members,'sampled_members':sampled,
            'nominal_capacity_bps':int(nominal),
            'utilization_percent':round(traffic/int(nominal)*100,2) if complete else None,
            'n1_capacity_bps':n1_capacity,
            'n1_utilization_percent':round(traffic/n1_capacity*100,2) if complete and n1_capacity>0 else None,
            'n1_over_capacity':traffic>n1_capacity if complete and n1_capacity>0 else None,
            'assumption':'All members active; loss of largest member; no rehashing or topology constraints modeled'})
    return result
