#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// LeKiwi console backend.
//
// Owns a single ZeroMQ PUSH socket feeding base-velocity commands to
// `lekiwi_host` (or the lighter `base_host.py`) on the Orin. The WebView cannot
// open a ZMQ socket itself, so command framing lives here; the frontend drives
// it over Tauri IPC.
//
// Why a dedicated runtime thread instead of calling the socket straight from
// async commands: the pure-Rust `zeromq` crate spawns a background IO task per
// socket and that task must be driven by a live tokio runtime for the lifetime
// of the connection. Driving it from Tauri's own async runtime proved
// unreliable (connect returns Ok but no TCP is ever established, so every send
// times out). Instead we run one owned `tokio` runtime on a plain std thread —
// exactly the environment a standalone binary has, which is known to work — and
// talk to it over channels. Commands become fire-and-forget messages; the
// worker owns the socket and does all awaiting.
//
// Wire contract (confirmed against lerobot 0.5.2 lekiwi_host / lekiwi.py, and
// matched by board/base_host.py): host binds a PULL socket on tcp://*:<port>
// (default 5555); each command is one JSON string
// {"x.vel": m/s, "y.vel": m/s, "theta.vel": deg/s}; the host filters ".vel"
// keys through _body_to_wheel_raw and stops the base if commands stop arriving.

use std::io::Write as _;
use std::time::{Duration, Instant};

use tauri::async_runtime::Mutex;
use tauri::{Emitter, Manager, State};
use tokio::sync::{mpsc, oneshot};
use zeromq::{PushSocket, Socket, SocketRecv, SocketSend, SubSocket, ZmqMessage};

/// Messages the frontend commands hand to the ZMQ worker thread.
enum Req {
    Connect(String, oneshot::Sender<Result<String, String>>),
    Send(f64, f64, f64),
    /// Pre-framed JSON (leader-arm follow messages).
    SendJson(String),
    Disconnect(oneshot::Sender<()>),
}

/// App state: the channel to the worker + a cached "connected endpoint" the UI
/// can read back. The worker is the sole owner of the socket.
struct Zmq {
    tx: mpsc::UnboundedSender<Req>,
    endpoint: Mutex<Option<String>>,
}

fn base_json(x: f64, y: f64, theta: f64) -> String {
    // Keys must be exactly x.vel/y.vel/theta.vel; the host indexes all three.
    format!("{{\"x.vel\": {x}, \"y.vel\": {y}, \"theta.vel\": {theta}}}")
}

/// The worker: one owned tokio runtime on its own thread, holding the socket.
fn spawn_worker() -> mpsc::UnboundedSender<Req> {
    let (tx, mut rx) = mpsc::unbounded_channel::<Req>();
    std::thread::spawn(move || {
        let rt = tokio::runtime::Runtime::new().expect("zmq worker runtime");
        rt.block_on(async move {
            let mut sock: Option<PushSocket> = None;
            while let Some(req) = rx.recv().await {
                match req {
                    Req::Connect(ep, reply) => {
                        let mut s = PushSocket::new();
                        let result = match s.connect(&ep).await {
                            Ok(_) => {
                                sock = Some(s);
                                Ok(ep)
                            }
                            Err(e) => Err(format!("connect failed: {e}")),
                        };
                        let _ = reply.send(result);
                    }
                    Req::Send(x, y, theta) => {
                        if let Some(s) = sock.as_mut() {
                            let msg = ZmqMessage::from(base_json(x, y, theta));
                            // Bounded so a stalled PUSH can't wedge the worker.
                            let _ = tokio::time::timeout(
                                Duration::from_millis(200),
                                s.send(msg),
                            )
                            .await;
                        }
                    }
                    Req::SendJson(json) => {
                        if let Some(s) = sock.as_mut() {
                            let _ = tokio::time::timeout(
                                Duration::from_millis(200),
                                s.send(ZmqMessage::from(json)),
                            )
                            .await;
                        }
                    }
                    Req::Disconnect(reply) => {
                        if let Some(s) = sock.as_mut() {
                            let zero = ZmqMessage::from(base_json(0.0, 0.0, 0.0));
                            let _ = tokio::time::timeout(
                                Duration::from_millis(200),
                                s.send(zero),
                            )
                            .await;
                        }
                        sock = None;
                        let _ = reply.send(());
                    }
                }
            }
        });
    });
    tx
}

// ---------------------------------------------------------------------------
// Generic log bus: a ZMQ SUB socket that subscribes to a board-side PUB (the
// gamepad daemon, and any future board process) on tcp://<ip>:5556. Each frame
// is one JSON line {"src","text"} which we forward verbatim to the frontend as
// a "log" event; the WebView's bottom panel timestamps and renders it. Same
// dedicated-runtime-thread rule as the PUSH socket. One-directional, disposable.

enum LogReq {
    Connect(String),
}

struct LogBus {
    tx: mpsc::UnboundedSender<LogReq>,
}

fn spawn_log_worker(app: tauri::AppHandle) -> mpsc::UnboundedSender<LogReq> {
    let (tx, mut rx) = mpsc::unbounded_channel::<LogReq>();
    std::thread::spawn(move || {
        let rt = tokio::runtime::Runtime::new().expect("log worker runtime");
        rt.block_on(async move {
            let mut sock: Option<SubSocket> = None;
            loop {
                tokio::select! {
                    cmd = rx.recv() => match cmd {
                        Some(LogReq::Connect(ep)) => {
                            let mut s = SubSocket::new();
                            if s.connect(&ep).await.is_ok() && s.subscribe("").await.is_ok() {
                                sock = Some(s);
                            }
                        }
                        None => break,   // app shutting down
                    },
                    msg = async { sock.as_mut().unwrap().recv().await }, if sock.is_some() => {
                        match msg {
                            Ok(m) => {
                                if let Some(bytes) = m.get(0) {
                                    if let Ok(text) = std::str::from_utf8(bytes) {
                                        let _ = app.emit("log", text.to_string());
                                    }
                                }
                            }
                            Err(_) => sock = None,   // link died; a reconnect re-subscribes
                        }
                    }
                }
            }
        });
    });
    tx
}

// ---------------------------------------------------------------------------
// Leader arm: a local Feetech STS3215 bus (SO-101 leader) read over USB serial.
// Same worker-thread pattern as the ZMQ socket: the port lives on its own
// thread; commands arrive over a channel; joint state streams to the frontend
// as "leader" events. While following, leader deltas from the aligned zero
// pose are pushed to base_host as {"arm.dq": [...]} via the ZMQ worker.

enum LReq {
    Connect(String, oneshot::Sender<Result<String, String>>),
    /// Capture the current pose as the zero reference (leader posed like the
    /// follower's rest pose).
    Align(oneshot::Sender<Result<(), String>>),
    Follow(bool),
    Disconnect,
}

struct Leader {
    tx: std::sync::mpsc::Sender<LReq>,
}

#[derive(Clone, serde::Serialize)]
struct LeaderFrame {
    connected: bool,
    following: bool,
    aligned: bool,
    joints: Vec<u16>,
}

/// One position read: FF FF id 04 02 38 02 cks -> FF FF id len err lo hi cks.
fn sts_read_pos(port: &mut Box<dyn serialport::SerialPort>, id: u8) -> Option<u16> {
    let body = [id, 4u8, 2, 56, 2];
    let cks = !(body.iter().map(|&b| b as u32).sum::<u32>() as u8);
    let mut pkt = vec![0xFFu8, 0xFF];
    pkt.extend_from_slice(&body);
    pkt.push(cks);
    let _ = port.clear(serialport::ClearBuffer::Input);
    port.write_all(&pkt).ok()?;
    port.flush().ok()?;
    let mut buf = [0u8; 32];
    let mut got = 0usize;
    let deadline = Instant::now() + Duration::from_millis(15);
    while Instant::now() < deadline {
        match port.read(&mut buf[got..]) {
            Ok(n) => {
                got += n;
                // Scan for FF FF id, need 8 bytes total from there.
                for i in 0..got.saturating_sub(6) {
                    if buf[i] == 0xFF && buf[i + 1] == 0xFF && buf[i + 2] == id && i + 7 <= got {
                        let lo = buf[i + 5] as u16;
                        let hi = buf[i + 6] as u16;
                        return Some((lo | (hi << 8)) & 0x0FFF);
                    }
                }
            }
            Err(_) => break,
        }
    }
    None
}

/// The aligned zero pose persists across launches; re-aligning overwrites it.
fn zero_path() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    std::path::PathBuf::from(home).join(".config/lekiwi-console/leader_zero.json")
}

fn load_zero() -> Option<[i32; 6]> {
    let text = std::fs::read_to_string(zero_path()).ok()?;
    let v: Vec<i32> = serde_json::from_str(&text).ok()?;
    v.try_into().ok()
}

fn save_zero(z: &[i32; 6]) {
    let p = zero_path();
    if let Some(dir) = p.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(p, serde_json::to_string(&z.to_vec()).unwrap_or_default());
}

fn spawn_leader(app: tauri::AppHandle, zmq_tx: mpsc::UnboundedSender<Req>) -> Leader {
    let (tx, rx) = std::sync::mpsc::channel::<LReq>();
    std::thread::spawn(move || {
        let mut port: Option<Box<dyn serialport::SerialPort>> = None;
        let mut zero: Option<[i32; 6]> = None;
        let mut following = false;
        loop {
            // The command channel doubles as the ~30 Hz tick clock.
            match rx.recv_timeout(Duration::from_millis(33)) {
                Ok(LReq::Connect(path, reply)) => {
                    match serialport::new(&path, 1_000_000)
                        .timeout(Duration::from_millis(10))
                        .open()
                    {
                        Ok(mut p) => {
                            // All six must answer or it's not a leader arm.
                            let ok = (1..=6u8).all(|id| sts_read_pos(&mut p, id).is_some());
                            if ok {
                                port = Some(p);
                                zero = load_zero();   // reuse last alignment
                                following = false;
                                let _ = reply.send(Ok(path));
                            } else {
                                let _ = reply.send(Err("主臂 1-6 号舵机未全部应答".into()));
                            }
                        }
                        Err(e) => {
                            let _ = reply.send(Err(format!("打开串口失败: {e}")));
                        }
                    }
                }
                Ok(LReq::Align(reply)) => {
                    let result = match port.as_mut() {
                        Some(p) => {
                            let mut z = [0i32; 6];
                            let mut ok = true;
                            for (i, id) in (1..=6u8).enumerate() {
                                match sts_read_pos(p, id) {
                                    Some(v) => z[i] = v as i32,
                                    None => ok = false,
                                }
                            }
                            if ok {
                                zero = Some(z);
                                save_zero(&z);
                                Ok(())
                            } else {
                                Err("读主臂关节失败".into())
                            }
                        }
                        None => Err("主臂未连接".into()),
                    };
                    let _ = reply.send(result);
                }
                Ok(LReq::Follow(on)) => following = on && zero.is_some(),
                Ok(LReq::Disconnect) => {
                    port = None;
                    zero = None;
                    following = false;
                    let _ = app.emit("leader", LeaderFrame {
                        connected: false,
                        following: false,
                        aligned: false,
                        joints: vec![],
                    });
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
            }
            if let Some(p) = port.as_mut() {
                let mut joints = Vec::with_capacity(6);
                for id in 1..=6u8 {
                    match sts_read_pos(p, id) {
                        Some(v) => joints.push(v),
                        None => break,
                    }
                }
                if joints.len() == 6 {
                    if following {
                        if let Some(z) = zero {
                            let dq: Vec<i32> = joints
                                .iter()
                                .enumerate()
                                .map(|(i, &v)| v as i32 - z[i])
                                .collect();
                            let json = format!(
                                "{{\"arm.dq\": [{}, {}, {}, {}, {}, {}]}}",
                                dq[0], dq[1], dq[2], dq[3], dq[4], dq[5]
                            );
                            let _ = zmq_tx.send(Req::SendJson(json));
                        }
                    }
                    let _ = app.emit("leader", LeaderFrame {
                        connected: true,
                        following,
                        aligned: zero.is_some(),
                        joints,
                    });
                } else {
                    // Port died (unplugged): drop it, stop following.
                    port = None;
                    zero = None;
                    following = false;
                    let _ = app.emit("leader", LeaderFrame {
                        connected: false,
                        following: false,
                        aligned: false,
                        joints: vec![],
                    });
                }
            }
        }
    });
    Leader { tx }
}

#[tauri::command]
async fn leader_connect(path: String, state: State<'_, Leader>) -> Result<String, String> {
    let (reply_tx, reply_rx) = oneshot::channel();
    state
        .tx
        .send(LReq::Connect(path, reply_tx))
        .map_err(|_| "leader worker is gone".to_string())?;
    reply_rx.await.map_err(|_| "leader worker dropped reply".to_string())?
}

#[tauri::command]
async fn leader_align(state: State<'_, Leader>) -> Result<(), String> {
    let (reply_tx, reply_rx) = oneshot::channel();
    state
        .tx
        .send(LReq::Align(reply_tx))
        .map_err(|_| "leader worker is gone".to_string())?;
    reply_rx.await.map_err(|_| "leader worker dropped reply".to_string())?
}

#[tauri::command]
fn leader_follow(on: bool, state: State<'_, Leader>) -> Result<(), String> {
    state
        .tx
        .send(LReq::Follow(on))
        .map_err(|_| "leader worker is gone".to_string())
}

#[tauri::command]
fn leader_disconnect(state: State<'_, Leader>) -> Result<(), String> {
    state
        .tx
        .send(LReq::Disconnect)
        .map_err(|_| "leader worker is gone".to_string())
}

/// Ask base_host to glide the follower arm to the calibrated middle pose
/// (alignment reference for leader follow).
#[tauri::command]
fn zmq_arm_mid(state: State<'_, Zmq>) -> Result<(), String> {
    state
        .tx
        .send(Req::SendJson("{\"arm.mid\": 1}".to_string()))
        .map_err(|_| "zmq worker is gone".to_string())
}

/// Fold the follower arm to REST, then cut its torque (same as the gamepad's
/// START button). Sent when follow stops, and from the standalone button.
#[tauri::command]
fn zmq_arm_relax(state: State<'_, Zmq>) -> Result<(), String> {
    state
        .tx
        .send(Req::SendJson("{\"arm.relax\": 1}".to_string()))
        .map_err(|_| "zmq worker is gone".to_string())
}

/// Point the log SUB at the board's PUB bus (same IP as the command socket,
/// fixed port 5556). Idempotent: re-issuing on reconnect just re-subscribes.
#[tauri::command]
fn log_connect(ip: String, state: State<'_, LogBus>) -> Result<(), String> {
    let ep = format!("tcp://{ip}:5556");
    state
        .tx
        .send(LogReq::Connect(ep))
        .map_err(|_| "log worker is gone".to_string())
}

/// Point the socket at lekiwi_host's command port. A wrong IP surfaces later as
/// commands that go nowhere, not as an error here — ZMQ connect is lazy.
#[tauri::command]
async fn zmq_connect(ip: String, port: u16, state: State<'_, Zmq>) -> Result<String, String> {
    let ep = format!("tcp://{ip}:{port}");
    let (reply_tx, reply_rx) = oneshot::channel();
    state
        .tx
        .send(Req::Connect(ep, reply_tx))
        .map_err(|_| "zmq worker is gone".to_string())?;
    let result = reply_rx.await.map_err(|_| "zmq worker dropped reply".to_string())?;
    if let Ok(ep) = &result {
        *state.endpoint.lock().await = Some(ep.clone());
    }
    result
}

/// Send one base-velocity command. Fire-and-forget: it only fails if the worker
/// thread itself has died, so the 20 Hz frontend stream never blocks on IO.
#[tauri::command]
async fn zmq_send_base(x: f64, y: f64, theta: f64, state: State<'_, Zmq>) -> Result<(), String> {
    state
        .tx
        .send(Req::Send(x, y, theta))
        .map_err(|_| "zmq worker is gone".to_string())
}

/// Stop the base (send a final zero) and drop the socket.
#[tauri::command]
async fn zmq_disconnect(state: State<'_, Zmq>) -> Result<(), String> {
    let (reply_tx, reply_rx) = oneshot::channel();
    state
        .tx
        .send(Req::Disconnect(reply_tx))
        .map_err(|_| "zmq worker is gone".to_string())?;
    let _ = reply_rx.await;
    *state.endpoint.lock().await = None;
    Ok(())
}

/// Endpoint currently connected, for the UI to reconcile its state.
#[tauri::command]
async fn zmq_status(state: State<'_, Zmq>) -> Result<Option<String>, String> {
    Ok(state.endpoint.lock().await.clone())
}

fn main() {
    let tx = spawn_worker();
    let zmq_tx_for_leader = tx.clone();
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|_app, _argv, _cwd| {}))
        .manage(Zmq {
            tx,
            endpoint: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            zmq_connect,
            zmq_send_base,
            zmq_disconnect,
            zmq_status,
            leader_connect,
            leader_align,
            leader_follow,
            leader_disconnect,
            zmq_arm_mid,
            zmq_arm_relax,
            log_connect,
        ])
        .setup(move |app| {
            app.manage(spawn_leader(app.handle().clone(), zmq_tx_for_leader));
            app.manage(LogBus {
                tx: spawn_log_worker(app.handle().clone()),
            });
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.set_focus();
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
