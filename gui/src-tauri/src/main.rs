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

use std::io::{Read as _, Write as _};
use std::time::{Duration, Instant};

use base64::Engine as _;

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
                    // Empty path = auto-discover: probe every /dev/serial/by-id
                    // entry; the leader arm is whichever answers for all six
                    // servo IDs at 1 Mbps (the base bus won't — different IDs).
                    let candidates: Vec<String> = if path.trim().is_empty() {
                        std::fs::read_dir("/dev/serial/by-id")
                            .map(|rd| {
                                rd.filter_map(|e| e.ok())
                                    .map(|e| e.path().to_string_lossy().into_owned())
                                    .collect()
                            })
                            .unwrap_or_default()
                    } else {
                        vec![path.clone()]
                    };
                    let mut result: Result<String, String> = Err(if path.trim().is_empty() {
                        "自动扫描: /dev/serial/by-id 下没有可用串口".into()
                    } else {
                        String::new()
                    });
                    for cand in candidates {
                        match serialport::new(&cand, 1_000_000)
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
                                    result = Ok(cand);
                                    break;
                                }
                                result = Err(format!("{cand}: 主臂 1-6 号舵机未全部应答"));
                            }
                            Err(e) => {
                                result = Err(format!("打开串口失败: {e}"));
                            }
                        }
                    }
                    let _ = reply.send(result);
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

// ---------------------------------------------------------------------------
// System telemetry for the top status bar. The GUI runs on the desktop, so the
// only channel to the Orin's own vitals is ssh (passwordless key already set
// up). One round-trip returns newline-delimited "key value..." lines the
// frontend parses; the ssh call runs on a blocking pool so the 4 s poll never
// stalls Tauri's async runtime.
//
// - temp  <max thermal-zone milli-°C>
// - cpu   <loadavg1> <nproc>
// - gpu   <per-mille 0..1000, or -1 if unreadable>
// - mem   <MemTotal_kB> <MemAvailable_kB>          (unified memory)
// - disk  <used_MB> <total_MB>   of /
// - pwr   <VDD_IN mV> <VDD_IN mA> from the INA3221   (board draw = host power)
// - sbatt <servo pack volts>  (base_host publishes it; empty when base is down)
const SYSINFO_SH: &str = concat!(
    "printf 'temp %s\\n' \"$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -rn | head -1)\";",
    "printf 'cpu %s %s\\n' \"$(cut -d' ' -f1 /proc/loadavg)\" \"$(nproc)\";",
    "printf 'gpu %s\\n' \"$(cat /sys/devices/platform/gpu.0/load 2>/dev/null || echo -1)\";",
    "awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print \"mem\",t,a}' /proc/meminfo;",
    "df -m / | awk 'NR==2{print \"disk\",$3,$2}';",
    "for h in /sys/class/hwmon/hwmon*; do if [ \"$(cat $h/name 2>/dev/null)\" = ina3221 ]; then ",
    "printf 'pwr %s %s\\n' \"$(cat $h/in1_input)\" \"$(cat $h/curr1_input)\"; break; fi; done;",
    "printf 'sbatt %s\\n' \"$(cat /tmp/lekiwi_batt 2>/dev/null)\";",
    "printf 'sarm %s\\n' \"$(cat /tmp/lekiwi_arm 2>/dev/null)\"",
);

/// SSH to the board and read its vitals. BatchMode=yes fails fast (no password
/// prompt) if the key isn't set up, surfacing as an error the UI shows offline.
#[tauri::command]
async fn sysinfo(ip: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let out = std::process::Command::new("ssh")
            .args([
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=6",
                "-o", "StrictHostKeyChecking=accept-new",
                &format!("jatson@{ip}"),
                SYSINFO_SH,
            ])
            .output()
            .map_err(|e| format!("ssh spawn failed: {e}"))?;
        if out.status.success() {
            Ok(String::from_utf8_lossy(&out.stdout).into_owned())
        } else {
            Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
        }
    })
    .await
    .map_err(|e| format!("sysinfo task failed: {e}"))?
}

/// Manual control of the vision services (vlm-daemon + llama-server), local or
/// over ssh. Runtime-only: start/stop/restart never touch the systemd enable
/// state, so boot autostart is unaffected. Stopping also frees llama's VRAM.
#[tauri::command]
async fn vlm_service(ip: String, action: String) -> Result<String, String> {
    if !matches!(
        action.as_str(),
        "start" | "stop" | "restart" | "enable" | "disable" | "is-enabled"
    ) {
        return Err(format!("bad action: {action}"));
    }
    tauri::async_runtime::spawn_blocking(move || {
        // is-enabled exits non-zero for "disabled", so don't let it fail the sh.
        let sc = format!(
            "systemctl --user {action} vlm-daemon.service llama-server.service{}",
            if action == "is-enabled" { " || true" } else { "" }
        );
        let out = if ip == "127.0.0.1" || ip == "localhost" {
            std::process::Command::new("sh").args(["-c", &sc]).output()
        } else {
            std::process::Command::new("ssh")
                .args([
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=6",
                    "-o", "StrictHostKeyChecking=accept-new",
                    &format!("jatson@{ip}"),
                    &sc,
                ])
                .output()
        }
        .map_err(|e| format!("spawn failed: {e}"))?;
        if out.status.success() {
            if action == "is-enabled" {
                Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
            } else {
                Ok(action)
            }
        } else {
            Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
        }
    })
    .await
    .map_err(|e| format!("vlm_service task failed: {e}"))?
}

// ---------------------------------------------------------------------------
// VLM daemon bridge. The camera + vision-language daemon (built separately)
// serves a small HTTP API on tcp://<ip>:8090, every endpoint guarded by a
// bearer token. The WebView must never see that token, so all HTTP lives here
// (mirroring the ssh telemetry pattern): blocking `ureq` on a spawn_blocking
// pool, token read fresh from a file the daemon writes. Any failure surfaces to
// the frontend as an error string, which the UI renders as the offline state
// and keeps probing health to reconnect.
//
// Endpoints (contract frozen):
//   GET  /health   -> {state, llama_up, camera, last_caption_ts, uptime}
//   GET  /frame.jpg-> latest JPEG bytes  (we return base64 for an <img> data URL)
//   GET  /caption  -> {text, frame_ts, latency_ms, seq}
//   POST /describe -> {text, frame_ts, latency_ms}   body {prompt?}
//   POST /state    -> body {state}
const VLM_PORT: u16 = 8090;

/// Token path the daemon generates. Read fresh on every call so a daemon
/// restart (new token) is picked up without relaunching the GUI. Missing/empty
/// file -> None, which the commands turn into an error the UI shows as offline.
fn vlm_token() -> Option<String> {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    let p = std::path::PathBuf::from(home).join("work/lekiwi-jatson-orin/vlm/token");
    std::fs::read_to_string(p)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn vlm_agent(secs: u64) -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(secs))
        .build()
}

fn vlm_url(ip: &str, path: &str) -> String {
    format!("http://{ip}:{VLM_PORT}{path}")
}

fn vlm_auth() -> Result<String, String> {
    vlm_token()
        .map(|t| format!("Bearer {t}"))
        .ok_or_else(|| "no VLM token (daemon not running?)".to_string())
}

/// GET a text/JSON endpoint, returning the raw body for the frontend to parse.
fn vlm_get_text(ip: &str, path: &str, secs: u64) -> Result<String, String> {
    let auth = vlm_auth()?;
    vlm_agent(secs)
        .get(&vlm_url(ip, path))
        .set("Authorization", &auth)
        .call()
        .map_err(|e| e.to_string())?
        .into_string()
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn vlm_health(ip: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || vlm_get_text(&ip, "/health", 5))
        .await
        .map_err(|e| format!("vlm task failed: {e}"))?
}

#[tauri::command]
async fn vlm_caption(ip: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || vlm_get_text(&ip, "/caption", 8))
        .await
        .map_err(|e| format!("vlm task failed: {e}"))?
}

/// Fetch the latest frame plus its metadata headers (X-Fps measured capture
/// rate, X-Frame-Ts capture wall time). Returns JSON {b64, fps, frame_ts}; the
/// frontend drops b64 into `img.src = "data:image/jpeg;base64,<...>"` and shows
/// the measured fps.
#[tauri::command]
async fn vlm_frame(ip: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let auth = vlm_auth()?;
        let resp = vlm_agent(6)
            .get(&vlm_url(&ip, "/frame.jpg"))
            .set("Authorization", &auth)
            .call()
            .map_err(|e| e.to_string())?;
        let fps: f64 = resp.header("X-Fps").and_then(|h| h.parse().ok()).unwrap_or(0.0);
        let frame_ts: f64 = resp.header("X-Frame-Ts").and_then(|h| h.parse().ok()).unwrap_or(0.0);
        let mut bytes = Vec::new();
        resp.into_reader()
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
        Ok(serde_json::json!({ "b64": b64, "fps": fps, "frame_ts": frame_ts }).to_string())
    })
    .await
    .map_err(|e| format!("vlm task failed: {e}"))?
}

/// One-shot describe. VLM inference can take several seconds, so the timeout is
/// generous; the call still runs off the async runtime.
#[tauri::command]
async fn vlm_describe(ip: String, prompt: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let auth = vlm_auth()?;
        let body = if prompt.trim().is_empty() {
            "{}".to_string()
        } else {
            serde_json::json!({ "prompt": prompt }).to_string()
        };
        vlm_agent(90)
            .post(&vlm_url(&ip, "/describe"))
            .set("Authorization", &auth)
            .set("Content-Type", "application/json")
            .send_string(&body)
            .map_err(|e| e.to_string())?
            .into_string()
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| format!("vlm task failed: {e}"))?
}

/// Promote/demote the daemon between "idle" and "watch" (continuous captioning).
#[tauri::command]
async fn vlm_set_state(ip: String, state: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let auth = vlm_auth()?;
        let body = serde_json::json!({ "state": state }).to_string();
        vlm_agent(5)
            .post(&vlm_url(&ip, "/state"))
            .set("Authorization", &auth)
            .set("Content-Type", "application/json")
            .send_string(&body)
            .map_err(|e| e.to_string())?
            .into_string()
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| format!("vlm task failed: {e}"))?
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
            sysinfo,
            leader_connect,
            leader_align,
            leader_follow,
            leader_disconnect,
            zmq_arm_mid,
            zmq_arm_relax,
            log_connect,
            vlm_health,
            vlm_service,
            vlm_frame,
            vlm_caption,
            vlm_describe,
            vlm_set_state,
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
