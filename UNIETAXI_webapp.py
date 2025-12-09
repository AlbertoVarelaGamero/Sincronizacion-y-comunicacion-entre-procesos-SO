"""
UNIETAXI - Versión PRO (corregida y estable)
Archivo: UNIETAXI_webapp_pro.py
Descripción:
  Versión corregida y estable de la app PRO. Esta edición fija errores de
  concurrencia, añade bloqueos correctos, protege import opcional (reportlab)
  y asegura que los taxis se mueven suavemente y que las rutas REST no fallan
  cuando el sistema no está iniciado.

Nota rápida:
  - Si instalas reportlab, la opción "Exportar PDF" funcionará; si no, el botón
    informa que la función no está disponible.

Requisitos mínimos:
  pip install flask
  (opcional) pip install reportlab

Ejecución:
  python UNIETAXI_webapp_pro.py
  Abrir http://127.0.0.1:5000
"""

from flask import Flask, jsonify, render_template_string, request, send_file
import threading, time, random, math, queue, io, os
from dataclasses import dataclass, field
from typing import Tuple, Dict, List, Optional

# Intento de importar reportlab (opcional). Si falla, desactivo export.
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# ================ CONFIG ================
ADMIN_USER = 'admin'
ADMIN_PASS = 'admin123'
DEFAULT_TAXIS = 5
DEFAULT_CLIENTS = 10
MATCH_RADIUS = 2.0
SERVICE_FEE_RATE = 0.20
MADRID_MIN_X, MADRID_MAX_X = 40.37, 40.50
MADRID_MIN_Y, MADRID_MAX_Y = -3.80, -3.63
DATA_DIR = 'unietaxi_reports'
os.makedirs(DATA_DIR, exist_ok=True)

# ================ MODELOS ================
@dataclass
class Taxi:
    taxi_id: int
    plate: str
    location: Tuple[float, float]
    rating: float = 4.5
    available: bool = True
    earnings: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    def distance_to(self, pos: Tuple[float, float]):
        return math.dist(self.location, pos)

@dataclass
class Client:
    client_id: int
    name: str
    location: Tuple[float, float]

@dataclass
class RequestObj:
    request_id: int
    client: Client
    origin: Tuple[float, float]
    destination: Tuple[float, float]
    timestamp: float
    assigned_taxi: Optional[int] = None
    completed: bool = False
    fare: float = 0.0

# ================ SISTEMA ================
class CentralSystem:
    def __init__(self):
        self.taxis: Dict[int, Taxi] = {}
        self.taxis_lock = threading.Lock()

        self.requests = queue.Queue()
        self.requests_index: Dict[int, RequestObj] = {}
        self.requests_lock = threading.Lock()
        self.request_counter = 0

        self.binary_semaphore = threading.Semaphore(1)

        self.completed_services: List[RequestObj] = []
        self.completed_lock = threading.Lock()

        self.company_monthly_profit = 0.0
        self.profit_lock = threading.Lock()

        self.new_request_condition = threading.Condition()
        self.adjudicator_thread = threading.Thread(target=self._adjudicator, daemon=True)
        self.running = False

        # time series for charts and lock
        self.time_series = {'timestamps': [], 'completed': [], 'profit': []}
        self.ts_lock = threading.Lock()

    def register_taxi(self, taxi: Taxi):
        with self.taxis_lock:
            self.taxis[taxi.taxi_id] = taxi

    def create_client_request(self, client: Client, origin: Tuple[float,float], dest: Tuple[float,float]) -> int:
        with self.requests_lock:
            self.request_counter += 1
            rid = self.request_counter
            r = RequestObj(request_id=rid, client=client, origin=origin, destination=dest, timestamp=time.time())
            self.requests.put(r)
            self.requests_index[rid] = r
        # notificar adjudicador
        with self.new_request_condition:
            self.new_request_condition.notify()
        return rid

    def start(self):
        self.running = True
        if not self.adjudicator_thread.is_alive():
            self.adjudicator_thread = threading.Thread(target=self._adjudicator, daemon=True)
            self.adjudicator_thread.start()

    def stop(self):
        self.running = False
        with self.new_request_condition:
            self.new_request_condition.notify_all()
        time.sleep(0.2)

    def _adjudicator(self):
        while self.running:
            with self.new_request_condition:
                while self.requests.empty() and self.running:
                    self.new_request_condition.wait(timeout=1)
                if not self.running:
                    break
                try:
                    req: RequestObj = self.requests.get_nowait()
                except queue.Empty:
                    continue

            acquired = self.binary_semaphore.acquire(timeout=3)
            if not acquired:
                # reencolar y seguir
                self.requests.put(req)
                continue
            try:
                assigned = self._assign_taxi(req)
            finally:
                self.binary_semaphore.release()

            if not assigned:
                # reintentar luego
                time.sleep(0.3)
                self.requests.put(req)

    def _assign_taxi(self, req: RequestObj) -> bool:
        candidates: List[tuple] = []
        with self.taxis_lock:
            for taxi in self.taxis.values():
                with taxi.lock:
                    if not taxi.available:
                        continue
                    d = taxi.distance_to(req.origin)
                    if d <= MATCH_RADIUS:
                        candidates.append((d, taxi))
        if not candidates:
            return False
        # ordenar por distancia y rating
        candidates.sort(key=lambda x: (x[0], -x[1].rating))
        chosen = candidates[0][1]
        with chosen.lock:
            if not chosen.available:
                return False
            chosen.available = False
        req.assigned_taxi = chosen.taxi_id
        threading.Thread(target=self._service, args=(req, chosen), daemon=True).start()
        return True

    def _service(self, req: RequestObj, taxi: Taxi):
        # movimiento suave (servidor actualiza ubicación paso a paso)
        def move_smooth(start, end, steps=20, delay=0.12):
            lat1, lon1 = start; lat2, lon2 = end
            dlat = (lat2 - lat1) / steps
            dlon = (lon2 - lon1) / steps
            for i in range(steps):
                with taxi.lock:
                    taxi.location = (lat1 + dlat*(i+1), lon1 + dlon*(i+1))
                time.sleep(delay)

        # 1. desplazarse hacia el cliente
        with taxi.lock:
            start_pos = taxi.location
        move_smooth(start_pos, req.origin, steps=15, delay=0.08)

        # 2. viaje cliente -> destino
        move_smooth(req.origin, req.destination, steps=25, delay=0.08)

        # finalizar servicio
        dist = math.dist(req.origin, req.destination)
        fare = round(dist * 1.6 + 2.0, 2)
        with taxi.lock:
            taxi.location = req.destination
            taxi.available = True
            taxi.earnings += fare * (1 - SERVICE_FEE_RATE)

        req.fare = fare
        req.completed = True
        with self.completed_lock:
            self.completed_services.append(req)
        with self.profit_lock:
            self.company_monthly_profit += fare * SERVICE_FEE_RATE

        with taxi.lock:
            taxi.rating = round((taxi.rating + random.uniform(3.7, 5.0)) / 2.0, 2)

        # actualizar series temporales de forma thread-safe
        ts = time.time()
        with self.ts_lock:
            self.time_series['timestamps'].append(ts)
            self.time_series['completed'].append(len(self.completed_services))
            self.time_series['profit'].append(self.company_monthly_profit)

    def get_snapshot(self):
        with self.taxis_lock, self.completed_lock, self.profit_lock, self.requests_lock:
            taxis = {tid: {'loc': t.location, 'rating': t.rating, 'avail': t.available, 'earnings': t.earnings} for tid, t in self.taxis.items()}
            pending = [ {'rid': r.request_id, 'client': r.client.name, 'origin': r.origin, 'destination': r.destination} for r in self.requests_index.values() if not r.completed and r.assigned_taxi is None ]
            # make a shallow copy of time series to avoid race
            with self.ts_lock:
                ts_copy = {k: list(v) for k, v in self.time_series.items()}
            return {'running': self.running, 'taxis': taxis, 'pending': pending, 'completed': len(self.completed_services), 'profit': self.company_monthly_profit, 'timeseries': ts_copy}

    def get_history(self):
        with self.completed_lock:
            return [{'rid': r.request_id, 'client': r.client.name, 'taxi': r.assigned_taxi, 'fare': r.fare} for r in self.completed_services]

# ================ UTILIDADES ================
def rand_madrid_point():
    return (round(random.uniform(MADRID_MIN_X, MADRID_MAX_X), 6), round(random.uniform(MADRID_MIN_Y, MADRID_MAX_Y), 6))

street_cache: Dict[str, Tuple[float,float]] = {}
def geocode_street(street: str) -> Tuple[float,float]:
    key = street.strip().lower()
    if key in street_cache:
        return street_cache[key]
    # generador determinista a partir del hash para reproducibilidad
    h = abs(hash(key))
    rx = MADRID_MIN_X + (h % 1000) / 1000.0 * (MADRID_MAX_X - MADRID_MIN_X)
    ry = MADRID_MIN_Y + ((h // 1000) % 1000) / 1000.0 * (MADRID_MAX_Y - MADRID_MIN_Y)
    pt = (round(rx, 6), round(ry, 6))
    street_cache[key] = pt
    return pt

# ================ FLASK APP ================
app = Flask(__name__)
central: Optional[CentralSystem] = None
client_threads: List[threading.Thread] = []
next_client_id = 1000
sessions: Dict[str, dict] = {}

HTML = r"""
<!doctype html>
<html lang='es' data-bs-theme='dark'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>UNIETAXI PRO - Madrid</title>
<link href='https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.2/css/bootstrap.min.css' rel='stylesheet'>
<link href='https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css' rel='stylesheet'>
<link rel='stylesheet' href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'/>
<style>
:root{--bg:#05060a;--card:#0e1721;--muted:#9aa4b2}
body{background:var(--bg);color:#e6eef6}
.navbar{background:linear-gradient(90deg,#071025,#0b1220)}
.card{background:var(--card);border:none}
#map{height:480px;border-radius:10px}
.small-muted{color:var(--muted)}
.taxi-icon{font-size:20px}
</style>
</head>
<body>
<nav class='navbar navbar-expand-lg navbar-dark px-3'>
  <a class='navbar-brand' href='#'><i class='fa-solid fa-taxi text-warning'></i> UNIETAXI PRO</a>
  <div class='ms-auto d-flex gap-2'>
    <button id='loginBtn' class='btn btn-outline-light btn-sm'>Admin</button>
    <button id='reportBtn' class='btn btn-outline-info btn-sm'>Exportar PDF</button>
    <button id='startBtn' class='btn btn-success btn-sm'><i class='fa fa-play'></i></button>
    <button id='stopBtn' class='btn btn-danger btn-sm'><i class='fa fa-stop'></i></button>
  </div>
</nav>

<div class='container-fluid my-3'>
  <div class='row gx-3'>
    <div class='col-lg-8'>
      <div class='card p-3 mb-3'>
        <h5>Mapa de Madrid</h5>
        <div id='map'></div>
      </div>
      <div class='card p-3 mb-3'>
        <h5>Gráficos</h5>
        <canvas id='chartCompleted' height='80'></canvas>
        <canvas id='chartProfit' height='80' class='mt-3'></canvas>
      </div>
    </div>
    <div class='col-lg-4'>
      <div class='card p-3 mb-3'>
        <h5>Estado del sistema</h5>
        <p id='state'><span class='badge bg-danger'>Detenido</span></p>
        <p class='small-muted'>Completados: <strong id='completed'>0</strong></p>
        <p class='small-muted'>Ganancia empresa: <strong id='profit'>S/0.00</strong></p>
      </div>
      <div class='card p-3 mb-3'>
        <h5>Pedir taxi</h5>
        <form id='reqForm'>
          <div class='mb-2'><label class='form-label small-muted'>Nombre</label><input id='name' class='form-control' required></div>
          <div class='mb-2'><label class='form-label small-muted'>Calle origen</label><input id='origin' class='form-control' placeholder='Ej: Gran Vía' required></div>
          <div class='mb-2'><label class='form-label small-muted'>Calle destino</label><input id='dest' class='form-control' placeholder='Ej: Calle de Alcalá' required></div>
          <button class='btn btn-primary w-100 mt-2'>Pedir ahora</button>
        </form>
      </div>
      <div class='card p-3'>
        <h5>Pendientes</h5>
        <div id='pending' class='small-muted'></div>
        <h5 class='mt-3'>Historial</h5>
        <div id='history' class='small-muted'></div>
      </div>
    </div>
  </div>
</div>

<!-- Login Modal -->
<div class='modal fade' id='loginModal' tabindex='-1'>
  <div class='modal-dialog'>
    <div class='modal-content'>
      <div class='modal-header'><h5 class='modal-title'>Login Administrador</h5><button type='button' class='btn-close' data-bs-dismiss='modal'></button></div>
      <div class='modal-body'>
        <div class='mb-2'><label class='form-label'>Usuario</label><input id='admUser' class='form-control' value='admin'></div>
        <div class='mb-2'><label class='form-label'>Contraseña</label><input id='admPass' type='password' class='form-control'></div>
      </div>
      <div class='modal-footer'><button id='admLoginBtn' class='btn btn-primary'>Entrar</button></div>
    </div>
  </div>
</div>

<script src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'></script>
<script src='https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.2/js/bootstrap.bundle.min.js'></script>
<script src='https://cdn.jsdelivr.net/npm/chart.js'></script>
<script>
const map = L.map('map').setView([40.4168, -3.7038], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19}).addTo(map);
const taxiMarkers = {};

function createOrMoveMarker(id, latlng){
  if(taxiMarkers[id]){
    // movimiento suave en cliente (complementario al servidor)
    const marker = taxiMarkers[id];
    const start = marker.getLatLng();
    const end = L.latLng(latlng[0], latlng[1]);
    const steps = 12; let i=0;
    const latStep = (end.lat - start.lat)/steps; const lngStep = (end.lng - start.lng)/steps;
    const iv = setInterval(()=>{ i++; marker.setLatLng([start.lat + latStep*i, start.lng + lngStep*i]); if(i>=steps) clearInterval(iv); }, 80);
  } else {
    const m = L.marker([latlng[0], latlng[1]], {title: 'Taxi '+id, icon: L.divIcon({className:'taxi-icon', html:'<i class="fa-solid fa-taxi text-warning"></i>'})}).addTo(map).bindPopup('Taxi '+id);
    taxiMarkers[id] = m;
  }
}

let chartC, chartP;
function initCharts(){
  const ctx1 = document.getElementById('chartCompleted').getContext('2d');
  chartC = new Chart(ctx1, {type:'line', data:{labels:[], datasets:[{label:'Servicios completados', data:[], fill:false, borderColor:'#4ade80'}]}, options:{scales:{x:{display:false}}}});
  const ctx2 = document.getElementById('chartProfit').getContext('2d');
  chartP = new Chart(ctx2, {type:'line', data:{labels:[], datasets:[{label:'Ganancia empresa', data:[], fill:true, backgroundColor:'rgba(59,130,246,0.15)', borderColor:'#3b82f6'}]}, options:{scales:{x:{display:false}}}});
}
initCharts();

async function refresh(){
  try{
    const r = await fetch('/status'); const j = await r.json();
    document.getElementById('state').innerHTML = j.running?"<span class='badge bg-success'>Ejecutando</span>":"<span class='badge bg-danger'>Detenido</span>";
    document.getElementById('completed').innerText = j.completed; document.getElementById('profit').innerText = 'S/'+j.profit.toFixed(2);
    for(const [id,t] of Object.entries(j.taxis)) createOrMoveMarker(id, t.loc);
    const pdiv = document.getElementById('pending'); pdiv.innerHTML=''; if(j.pending.length===0) pdiv.innerHTML='<em>Sin pendientes</em>';
    j.pending.forEach(x=>{ const el=document.createElement('div'); el.className='border-bottom py-1'; el.innerHTML=`<strong>#${x.rid}</strong> ${x.client}`; pdiv.appendChild(el); });
    const hres = await fetch('/history'); const hj = await hres.json(); const hdiv=document.getElementById('history'); hdiv.innerHTML=''; hj.slice(-8).reverse().forEach(x=>hdiv.innerHTML+=`<div class='text-muted small mb-1'>#${x.rid} ${x.client} → Taxi ${x.taxi} - S/${x.fare}</div>`);
    // charts
    const ts = j.timeseries || {timestamps:[], completed:[], profit:[]};
    chartC.data.labels = ts.timestamps.map(t=>new Date(t*1000).toLocaleTimeString()); chartC.data.datasets[0].data = ts.completed; chartC.update();
    chartP.data.labels = ts.timestamps.map(t=>new Date(t*1000).toLocaleTimeString()); chartP.data.datasets[0].data = ts.profit; chartP.update();
  }catch(e){ console.error('refresh error', e); }
}
setInterval(refresh, 1200); refresh();

// controles
document.getElementById('startBtn').onclick = ()=>fetch('/start',{method:'POST'});
document.getElementById('stopBtn').onclick = ()=>fetch('/stop',{method:'POST'});
document.getElementById('reportBtn').onclick = async ()=>{ const res = await fetch('/export_report'); const j = await res.json(); if(j.url) window.open(j.url,'_blank'); else alert('Export no disponible'); };

// login modal bootstrap
const loginModal = new bootstrap.Modal(document.getElementById('loginModal'));
document.getElementById('loginBtn').onclick = ()=> loginModal.show();
document.getElementById('admLoginBtn').onclick = async ()=>{
  const user=document.getElementById('admUser').value; const pass=document.getElementById('admPass').value;
  const r = await fetch('/admin_login',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user, pass})}); const j = await r.json();
  if(j.ok){ loginModal.hide(); alert('Login correcto'); } else alert('Credenciales inválidas');
}

// request form
document.getElementById('reqForm').onsubmit = async (e)=>{
  e.preventDefault();
  const name = document.getElementById('name').value; const origin = document.getElementById('origin').value; const dest = document.getElementById('dest').value;
  const r = await fetch('/request',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, origin, destination: dest})});
  const j = await r.json();
  if(j.rid) alert('Solicitud enviada #' + j.rid); else alert('Error: ' + (j.error||'no disponible'));
}
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/start', methods=['POST'])
def start_sim():
    global central, client_threads, next_client_id
    if central and central.running:
        return jsonify({'ok': True})
    central = CentralSystem()
    # crear taxis y clientes
    taxis = [Taxi(i, f"TAXI-{100+i}", rand_madrid_point()) for i in range(1, DEFAULT_TAXIS+1)]
    for t in taxis: central.register_taxi(t)
    clients = [Client(i, f"Cliente-{i}", rand_madrid_point()) for i in range(1, DEFAULT_CLIENTS+1)]
    client_threads = [threading.Thread(target=lambda c=c: central.create_client_request(c, c.location, rand_madrid_point()), daemon=True) for c in clients]
    for ct in client_threads: ct.start()
    central.start(); next_client_id = 1000
    return jsonify({'ok': True})

@app.route('/stop', methods=['POST'])
def stop_sim():
    global central, client_threads
    if central:
        central.stop()
        for ct in client_threads:
            try: ct.join(0.01)
            except: pass
        central = None; client_threads = []
    return jsonify({'ok': True})

@app.route('/status')
def status():
    if not central:
        return jsonify({'running': False, 'taxis': {}, 'pending': [], 'completed': 0, 'profit': 0.0, 'timeseries': {'timestamps': [], 'completed': [], 'profit': []}})
    snap = central.get_snapshot()
    return jsonify(snap)

@app.route('/history')
def history():
    if not central: return jsonify([])
    return jsonify(central.get_history())

@app.route('/request', methods=['POST'])
def create_request():
    global central, next_client_id
    if not central or not central.running:
        return jsonify({'error': 'Sistema no está en ejecución'})
    data = request.get_json() or {}
    name = data.get('name', 'Anonimo')
    origin_str = data.get('origin', '')
    dest_str = data.get('destination', '')
    origin = geocode_street(origin_str)
    dest = geocode_street(dest_str)
    client = Client(next_client_id, name, origin)
    next_client_id += 1
    rid = central.create_client_request(client, origin, dest)
    return jsonify({'rid': rid})

@app.route('/admin_login', methods=['POST'])
def admin_login():
    data = request.get_json() or {}
    user = data.get('user'); pwd = data.get('pass')
    if user == ADMIN_USER and pwd == ADMIN_PASS:
        token = str(time.time())
        sessions[token] = {'user': user, 'login': time.time()}
        return jsonify({'ok': True, 'token': token})
    return jsonify({'ok': False})

@app.route('/export_report')
def export_report():
    if not REPORTLAB_AVAILABLE:
        return jsonify({'error': 'reportlab not installed'})
    if not central:
        return jsonify({'error': 'no_running'})
    timestamp = int(time.time())
    filename = os.path.join(DATA_DIR, f'unietaxi_report_{timestamp}.pdf')
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    c.setFont('Helvetica-Bold', 16); c.drawString(40, height-60, 'UNIETAXI - Informe de Simulación')
    c.setFont('Helvetica', 10); c.drawString(40, height-80, time.ctime())
    snap = central.get_snapshot()
    c.drawString(40, height-110, f"Taxis registrados: {len(snap['taxis'])}")
    c.drawString(40, height-125, f"Servicios completados: {snap['completed']}")
    c.drawString(40, height-140, f"Ganancia empresa: S/{snap['profit']:.2f}")
    y = height-170
    c.setFont('Helvetica-Bold', 12); c.drawString(40, y, 'Resumen por taxi:'); y -= 18; c.setFont('Helvetica', 9)
    for tid, t in snap['taxis'].items():
        c.drawString(48, y, f"Taxi {tid} - loc: {tuple(round(x,4) for x in t['loc'])} - rating: {t['rating']} - earnings: S/{t['earnings']:.2f}")
        y -= 14
        if y < 80:
            c.showPage(); y = height-60
    c.showPage(); c.save()
    buffer.seek(0)
    with open(filename, 'wb') as f: f.write(buffer.read())
    return jsonify({'url': f'/download/{os.path.basename(filename)}'})

@app.route('/download/<path:fn>')
def download(fn):
    path = os.path.join(DATA_DIR, fn)
    if not os.path.exists(path):
        return 'Not found', 404
    return send_file(path, as_attachment=True)

if __name__ == '__main__':
    print('UNIETAXI PRO (corregido) running at http://127.0.0.1:5000')
    app.run(debug=False)
