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
    // src feeds base_host's priority mux: pad > gui > mcp.
    format!("{{\"src\": \"gui\", \"x.vel\": {x}, \"y.vel\": {y}, \"theta.vel\": {theta}}}")
}

/// Send one frame with a 200 ms ceiling so a stalled PUSH can't wedge the
/// worker. A timeout/error means the peer is gone (base_host restarted): drop
/// the socket so the self-heal probe rebuilds it. The pure-Rust `zeromq` PUSH
/// does NOT reconnect on its own — without this a board reboot silently wedges
/// every command (base drive AND arm follow) while the UI still shows 已连接.
async fn push_or_drop(sock: &mut Option<PushSocket>, payload: String) {
    if let Some(s) = sock.as_mut() {
        let ok = matches!(
            tokio::time::timeout(
                Duration::from_millis(200),
                s.send(ZmqMessage::from(payload))
            )
            .await,
            Ok(Ok(())),
        );
        if !ok {
            *sock = None;
        }
    }
}

/// The worker: one owned tokio runtime on its own thread, holding the socket.
/// `ep` is the DESIRED endpoint (set on Connect, cleared on Disconnect); while
/// it is set but the socket is down, a 2 s probe reconnects — same self-heal the
/// log bus uses, so a board reboot recovers the command channel automatically.
fn spawn_worker() -> mpsc::UnboundedSender<Req> {
    let (tx, mut rx) = mpsc::unbounded_channel::<Req>();
    std::thread::spawn(move || {
        let rt = tokio::runtime::Runtime::new().expect("zmq worker runtime");
        rt.block_on(async move {
            let mut sock: Option<PushSocket> = None;
            let mut ep: Option<String> = None;
            loop {
                tokio::select! {
                    req = rx.recv() => match req {
                        None => break,   // app shutting down
                        Some(Req::Connect(e, reply)) => {
                            ep = Some(e.clone());
                            let mut s = PushSocket::new();
                            let result = match s.connect(&e).await {
                                Ok(_) => { sock = Some(s); Ok(e) }
                                Err(err) => { sock = None; Err(format!("connect failed: {err}")) }
                            };
                            let _ = reply.send(result);
                        }
                        Some(Req::Send(x, y, theta)) => {
                            push_or_drop(&mut sock, base_json(x, y, theta)).await;
                        }
                        Some(Req::SendJson(json)) => {
                            push_or_drop(&mut sock, json).await;
                        }
                        Some(Req::Disconnect(reply)) => {
                            if sock.is_some() {
                                push_or_drop(&mut sock, base_json(0.0, 0.0, 0.0)).await;
                            }
                            ep = None;      // intentional: stop the self-heal probe
                            sock = None;
                            let _ = reply.send(());
                        }
                    },
                    // Self-heal: reconnect every 2 s while an endpoint is wanted but
                    // the socket is down (peer restarted / initial connect raced).
                    _ = tokio::time::sleep(Duration::from_secs(2)),
                        if sock.is_none() && ep.is_some() =>
                    {
                        if let Some(e) = &ep {
                            let mut s = PushSocket::new();
                            if s.connect(e).await.is_ok() {
                                sock = Some(s);
                            }
                        }
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
            let mut ep: Option<String> = None;
            loop {
                tokio::select! {
                    cmd = rx.recv() => match cmd {
                        Some(LogReq::Connect(e)) => { ep = Some(e); sock = None; }
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
                            Err(_) => sock = None,   // link died; retried below
                        }
                    },
                    // self-heal: a board reboot must not leave the log strip
                    // silently dead — probe every 2s while down
                    _ = tokio::time::sleep(Duration::from_secs(2)),
                        if sock.is_none() && ep.is_some() => {}
                }
                if sock.is_none() {
                    if let Some(e) = &ep {
                        let mut s = SubSocket::new();
                        if s.connect(e).await.is_ok() && s.subscribe("").await.is_ok() {
                            sock = Some(s);
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
    /// Three-stage LeRobot-compatible calibration: start -> middle -> finish.
    Calibrate(String, oneshot::Sender<Result<String, String>>),
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

#[derive(Clone, Copy)]
struct ServoCalibration {
    offset: i32,
    min: u16,
    max: u16,
}

#[derive(Clone, Copy, PartialEq)]
enum CalibrationStage {
    Middle,
    Range,
}

struct LeaderCalibration {
    stage: CalibrationStage,
    original: [ServoCalibration; 6],
    offsets: [i32; 6],
    mins: [u16; 6],
    maxes: [u16; 6],
}

fn sts_read(
    port: &mut Box<dyn serialport::SerialPort>,
    id: u8,
    addr: u8,
    len: u8,
) -> Option<Vec<u8>> {
    let body = [id, 4u8, 2, addr, len];
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
                let need = len as usize;
                for i in 0..got.saturating_sub(5) {
                    if buf[i] == 0xFF
                        && buf[i + 1] == 0xFF
                        && buf[i + 2] == id
                        && i + 5 + need <= got
                    {
                        return Some(buf[i + 5..i + 5 + need].to_vec());
                    }
                }
            }
            Err(_) => break,
        }
    }
    None
}

fn sts_read_u16(port: &mut Box<dyn serialport::SerialPort>, id: u8, addr: u8) -> Option<u16> {
    let b = sts_read(port, id, addr, 2)?;
    Some(b[0] as u16 | ((b[1] as u16) << 8))
}

/// One position read: FF FF id 04 02 38 02 cks -> FF FF id len err lo hi cks.
fn sts_read_pos(port: &mut Box<dyn serialport::SerialPort>, id: u8) -> Option<u16> {
    Some(sts_read_u16(port, id, 56)? & 0x0FFF)
}

fn sts_write(
    port: &mut Box<dyn serialport::SerialPort>,
    id: u8,
    addr: u8,
    data: &[u8],
) -> Result<(), String> {
    let mut body = vec![id, data.len() as u8 + 3, 3, addr];
    body.extend_from_slice(data);
    let cks = !(body.iter().map(|&b| b as u32).sum::<u32>() as u8);
    let mut pkt = vec![0xFF, 0xFF];
    pkt.extend_from_slice(&body);
    pkt.push(cks);
    let _ = port.clear(serialport::ClearBuffer::Input);
    port.write_all(&pkt).map_err(|e| e.to_string())?;
    port.flush().map_err(|e| e.to_string())?;
    std::thread::sleep(Duration::from_millis(2));
    Ok(())
}

fn sts_write_u16(
    port: &mut Box<dyn serialport::SerialPort>,
    id: u8,
    addr: u8,
    value: u16,
) -> Result<(), String> {
    sts_write(port, id, addr, &[value as u8, (value >> 8) as u8])
}

fn encode_sign_magnitude(value: i32, sign_bit: u32) -> Result<u16, String> {
    let max = (1i32 << sign_bit) - 1;
    if value.abs() > max {
        return Err(format!("offset {value} exceeds ±{max}"));
    }
    Ok(value.unsigned_abs() as u16 | if value < 0 { 1u16 << sign_bit } else { 0 })
}

fn decode_sign_magnitude(value: u16, sign_bit: u32) -> i32 {
    let magnitude = value & ((1u16 << sign_bit) - 1);
    if value & (1u16 << sign_bit) != 0 {
        -(magnitude as i32)
    } else {
        magnitude as i32
    }
}

fn read_servo_calibration(
    port: &mut Box<dyn serialport::SerialPort>,
    id: u8,
) -> Result<ServoCalibration, String> {
    let offset = sts_read_u16(port, id, 31)
        .map(|v| decode_sign_magnitude(v, 11))
        .ok_or_else(|| format!("舵机 {id} 读取中位偏移失败"))?;
    let min = sts_read_u16(port, id, 9).ok_or_else(|| format!("舵机 {id} 读取最小限位失败"))?;
    let max = sts_read_u16(port, id, 11).ok_or_else(|| format!("舵机 {id} 读取最大限位失败"))?;
    Ok(ServoCalibration { offset, min, max })
}

fn write_servo_calibration(
    port: &mut Box<dyn serialport::SerialPort>,
    id: u8,
    cal: ServoCalibration,
) -> Result<(), String> {
    sts_write(port, id, 55, &[0])?;
    sts_write_u16(port, id, 31, encode_sign_magnitude(cal.offset, 11)?)?;
    sts_write_u16(port, id, 9, cal.min)?;
    sts_write_u16(port, id, 11, cal.max)?;
    sts_write(port, id, 55, &[1])
}

/// The aligned zero pose persists across launches; re-aligning overwrites it.
fn zero_path() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    std::path::PathBuf::from(home).join(".config/lekiwi-console/leader_zero.json")
}

/// Hand-editable machine config (board IP, ports, topics). Lives next to
/// leader_zero.json so all persisted state shares one dir; survives release
/// builds where ui/ assets are baked into the binary. Missing file -> "{}"
/// so the frontend seeds nothing and falls back to its own defaults.
fn config_path() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    std::path::PathBuf::from(home).join(".config/lekiwi-console/config.json")
}

#[tauri::command]
fn load_config() -> String {
    std::fs::read_to_string(config_path()).unwrap_or_else(|_| "{}".into())
}

/// Directory holding the daemon token files (vlm/token, voice/token, synced
/// from the board). Config key "tokenDir" points at the repo checkout; the
/// legacy ~/work/lekiwi-jetson-orin default kept as fallback.
fn token_dir() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    let cfg: serde_json::Value =
        serde_json::from_str(&load_config()).unwrap_or(serde_json::Value::Null);
    if let Some(d) = cfg
        .get("tokenDir")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
    {
        let d = d
            .strip_prefix("~/")
            .map(|rest| format!("{home}/{rest}"))
            .unwrap_or_else(|| d.to_string());
        return std::path::PathBuf::from(d);
    }
    std::path::PathBuf::from(home).join("work/lekiwi-jetson-orin")
}

/// The config file is the ONLY store for connection params — GUI edits write
/// back here (no localStorage second truth). The frontend sends the full merged
/// JSON text; fs::write follows the symlink so a repo-side config.local.json
/// stays the single hand-editable copy.
#[tauri::command]
fn save_config(text: String) -> Result<(), String> {
    // The file is the single source of truth with no backup — refuse to
    // clobber it with something a frontend bug produced.
    serde_json::from_str::<serde_json::Value>(&text)
        .map_err(|e| format!("refusing to save invalid JSON: {e}"))?;
    let p = config_path();
    if let Some(d) = p.parent() {
        std::fs::create_dir_all(d).map_err(|e| e.to_string())?;
    }
    // config.json is a symlink to the repo's config.local.json; rename onto the
    // symlink itself would replace it with a regular file and fork the two
    // copies, so resolve to the real target first.
    let p = std::fs::canonicalize(&p).unwrap_or(p);
    // Write-then-rename, never truncate in place: this file is re-read by every
    // vlm/voice request to resolve tokenDir, so a reader landing inside a
    // truncate window would parse "" -> lose tokenDir -> "no VLM token" -> the
    // GUI flips 离线 for no reason. rename(2) is atomic on the same filesystem.
    let tmp = p.with_extension("json.tmp");
    std::fs::write(&tmp, text).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &p).map_err(|e| e.to_string())
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

fn leader_calibration_path() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    std::path::PathBuf::from(home).join(".config/lekiwi-console/leader_calibration.json")
}

fn save_leader_calibration(cal: &LeaderCalibration) -> Result<(), String> {
    let names = [
        "shoulder_pan",
        "shoulder_lift",
        "elbow_flex",
        "wrist_flex",
        "wrist_roll",
        "gripper",
    ];
    let mut root = serde_json::Map::new();
    for (i, name) in names.iter().enumerate() {
        root.insert(
            (*name).into(),
            serde_json::json!({
                "id": i + 1,
                "drive_mode": 0,
                "homing_offset": cal.offsets[i],
                "range_min": cal.mins[i],
                "range_max": cal.maxes[i],
            }),
        );
    }
    let path = leader_calibration_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let tmp = path.with_extension("json.tmp");
    let text = serde_json::to_string_pretty(&serde_json::Value::Object(root))
        .map_err(|e| e.to_string())?;
    std::fs::write(&tmp, text + "\n").map_err(|e| e.to_string())?;
    std::fs::rename(tmp, path).map_err(|e| e.to_string())
}

fn spawn_leader(app: tauri::AppHandle, zmq_tx: mpsc::UnboundedSender<Req>) -> Leader {
    let (tx, rx) = std::sync::mpsc::channel::<LReq>();
    std::thread::spawn(move || {
        let mut port: Option<Box<dyn serialport::SerialPort>> = None;
        let mut zero: Option<[i32; 6]> = None;
        let mut following = false;
        let mut calibration: Option<LeaderCalibration> = None;
        loop {
            // The command channel doubles as the ~30 Hz tick clock.
            match rx.recv_timeout(Duration::from_millis(33)) {
                Ok(LReq::Connect(path, reply)) => {
                    // Empty path = auto-discover; the leader arm is whichever
                    // port answers for all six servo IDs at 1 Mbps (the base
                    // bus won't — different IDs). The GUI runs on the operator's
                    // desktop, which is NOT necessarily Linux: /dev/serial/by-id
                    // exists only there, so ask serialport for the real list and
                    // keep by-id paths first on Linux (stable across replug).
                    let candidates: Vec<String> = if path.trim().is_empty() {
                        let mut v: Vec<String> = std::fs::read_dir("/dev/serial/by-id")
                            .map(|rd| {
                                rd.filter_map(|e| e.ok())
                                    .map(|e| e.path().to_string_lossy().into_owned())
                                    .collect()
                            })
                            .unwrap_or_default();
                        v.extend(
                            serialport::available_ports()
                                .unwrap_or_default()
                                .into_iter()
                                // USB only: skip Bluetooth-Incoming-Port and the
                                // debug console, which open fine and time out.
                                .filter(|p| {
                                    matches!(p.port_type, serialport::SerialPortType::UsbPort(_))
                                })
                                .map(|p| p.port_name),
                        );
                        v
                    } else {
                        vec![path.clone()]
                    };
                    let mut result: Result<String, String> = Err(if path.trim().is_empty() {
                        "自动扫描: 没有发现任何 USB 串口（主臂没插这台电脑？换个口/线试试）".into()
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
                                    if let Some(old) = calibration.take() {
                                        for (i, cal) in old.original.iter().enumerate() {
                                            let _ =
                                                write_servo_calibration(&mut p, i as u8 + 1, *cal);
                                        }
                                    }
                                    port = Some(p);
                                    zero = load_zero(); // reuse last alignment
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
                Ok(LReq::Calibrate(action, reply)) => {
                    following = false;
                    let result = (|| -> Result<String, String> {
                        match action.as_str() {
                            "start" => match port.as_mut() {
                                None => Err("主臂未连接".into()),
                                Some(_) if calibration.is_some() => Err("主臂校准已经开始".into()),
                                Some(p) => {
                                    let mut original = [ServoCalibration {
                                        offset: 0,
                                        min: 0,
                                        max: 4095,
                                    }; 6];
                                    let mut error = None;
                                    for id in 1..=6u8 {
                                        match read_servo_calibration(p, id) {
                                            Ok(cal) => original[id as usize - 1] = cal,
                                            Err(e) => {
                                                error = Some(e);
                                                break;
                                            }
                                        }
                                    }
                                    if let Some(e) = error {
                                        Err(e)
                                    } else {
                                        for id in 1..=6u8 {
                                            sts_write(p, id, 40, &[0])?;
                                        }
                                        calibration = Some(LeaderCalibration {
                                            stage: CalibrationStage::Middle,
                                            original,
                                            offsets: [0; 6],
                                            mins: [2047; 6],
                                            maxes: [2047; 6],
                                        });
                                        Ok("middle".into())
                                    }
                                }
                            },
                            "middle" => match (port.as_mut(), calibration.as_mut()) {
                                (None, _) => Err("主臂未连接".into()),
                                (_, None) => Err("主臂校准尚未开始".into()),
                                (Some(_), Some(c)) if c.stage != CalibrationStage::Middle => {
                                    Err("已经记录过中位".into())
                                }
                                (Some(p), Some(c)) => {
                                    let reset = ServoCalibration {
                                        offset: 0,
                                        min: 0,
                                        max: 4095,
                                    };
                                    for id in 1..=6u8 {
                                        write_servo_calibration(p, id, reset)?;
                                    }
                                    for id in 1..=6u8 {
                                        let i = id as usize - 1;
                                        let actual = sts_read_pos(p, id)
                                            .ok_or_else(|| format!("舵机 {id} 读取中位失败"))?;
                                        c.offsets[i] = actual as i32 - 2047;
                                        write_servo_calibration(
                                            p,
                                            id,
                                            ServoCalibration {
                                                offset: c.offsets[i],
                                                min: 0,
                                                max: 4095,
                                            },
                                        )?;
                                    }
                                    for id in 1..=6u8 {
                                        let i = id as usize - 1;
                                        let pos = sts_read_pos(p, id)
                                            .ok_or_else(|| format!("舵机 {id} 中位复读失败"))?;
                                        c.mins[i] = pos;
                                        c.maxes[i] = pos;
                                    }
                                    c.stage = CalibrationStage::Range;
                                    Ok("range".into())
                                }
                            },
                            "finish" => match (port.as_mut(), calibration.as_mut()) {
                                (None, _) => Err("主臂未连接".into()),
                                (_, None) => Err("主臂校准尚未开始".into()),
                                (Some(_), Some(c)) if c.stage != CalibrationStage::Range => {
                                    Err("还没有记录中位".into())
                                }
                                (Some(p), Some(c)) => {
                                    let narrow: Vec<String> = (0..6)
                                        .filter(|&i| c.maxes[i] - c.mins[i] < 100)
                                        .map(|i| format!("{}", i + 1))
                                        .collect();
                                    if !narrow.is_empty() {
                                        Err(format!(
                                            "这些关节活动范围不足：{}；继续摆到两端后再完成",
                                            narrow.join(", ")
                                        ))
                                    } else {
                                        for id in 1..=6u8 {
                                            let i = id as usize - 1;
                                            write_servo_calibration(
                                                p,
                                                id,
                                                ServoCalibration {
                                                    offset: c.offsets[i],
                                                    min: c.mins[i],
                                                    max: c.maxes[i],
                                                },
                                            )?;
                                        }
                                        save_leader_calibration(c)?;
                                        let middle = [2047i32; 6];
                                        zero = Some(middle);
                                        save_zero(&middle);
                                        calibration = None;
                                        Ok("saved".into())
                                    }
                                }
                            },
                            "cancel" => match port.as_mut() {
                                None => Err("主臂未连接".into()),
                                Some(p) => {
                                    if let Some(c) = calibration.take() {
                                        for (i, old) in c.original.iter().enumerate() {
                                            write_servo_calibration(p, i as u8 + 1, *old)?;
                                        }
                                    }
                                    Ok("cancelled".into())
                                }
                            },
                            _ => Err(format!("未知校准动作：{action}")),
                        }
                    })();
                    let _ = reply.send(result);
                }
                Ok(LReq::Follow(on)) => following = on && zero.is_some(),
                Ok(LReq::Disconnect) => {
                    if let (Some(p), Some(c)) = (port.as_mut(), calibration.take()) {
                        for (i, old) in c.original.iter().enumerate() {
                            let _ = write_servo_calibration(p, i as u8 + 1, *old);
                        }
                    }
                    port = None;
                    zero = None;
                    following = false;
                    let _ = app.emit(
                        "leader",
                        LeaderFrame {
                            connected: false,
                            following: false,
                            aligned: false,
                            joints: vec![],
                        },
                    );
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
                    if let Some(c) = calibration.as_mut() {
                        if c.stage == CalibrationStage::Range {
                            for (i, &pos) in joints.iter().enumerate() {
                                c.mins[i] = c.mins[i].min(pos);
                                c.maxes[i] = c.maxes[i].max(pos);
                            }
                        }
                    }
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
                    let _ = app.emit(
                        "leader",
                        LeaderFrame {
                            connected: true,
                            following,
                            aligned: zero.is_some(),
                            joints,
                        },
                    );
                } else {
                    // Port died (unplugged): drop it, stop following.
                    port = None;
                    zero = None;
                    following = false;
                    let _ = app.emit(
                        "leader",
                        LeaderFrame {
                            connected: false,
                            following: false,
                            aligned: false,
                            joints: vec![],
                        },
                    );
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
    reply_rx
        .await
        .map_err(|_| "leader worker dropped reply".to_string())?
}

#[tauri::command]
async fn leader_align(state: State<'_, Leader>) -> Result<(), String> {
    let (reply_tx, reply_rx) = oneshot::channel();
    state
        .tx
        .send(LReq::Align(reply_tx))
        .map_err(|_| "leader worker is gone".to_string())?;
    reply_rx
        .await
        .map_err(|_| "leader worker dropped reply".to_string())?
}

#[tauri::command]
async fn leader_calibrate(action: String, state: State<'_, Leader>) -> Result<String, String> {
    if !matches!(action.as_str(), "start" | "middle" | "finish" | "cancel") {
        return Err(format!("bad calibration action: {action}"));
    }
    let (reply_tx, reply_rx) = oneshot::channel();
    state
        .tx
        .send(LReq::Calibrate(action, reply_tx))
        .map_err(|_| "leader worker is gone".to_string())?;
    reply_rx
        .await
        .map_err(|_| "leader worker dropped reply".to_string())?
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

#[tauri::command]
fn zmq_arm_calibrate(action: String, seq: u64, state: State<'_, Zmq>) -> Result<(), String> {
    if !matches!(action.as_str(), "start" | "middle" | "finish" | "cancel") {
        return Err(format!("bad calibration action: {action}"));
    }
    state
        .tx
        .send(Req::SendJson(format!(
            "{{\"arm.calibrate\": {}, \"cal.seq\": {seq}}}",
            serde_json::to_string(&action).map_err(|e| e.to_string())?
        )))
        .map_err(|_| "zmq worker is gone".to_string())
}

/// Latch base_host's safety master switch. on=false cuts torque on all nine
/// motors while the command chain — recv, priority mux, telemetry — keeps
/// running for debugging.
/// Actual state is echoed back via sysinfo's `motion` line, not assumed here.
#[tauri::command]
fn zmq_set_motion(on: bool, state: State<'_, Zmq>) -> Result<(), String> {
    let v = if on { 1 } else { 0 };
    state
        .tx
        .send(Req::SendJson(format!("{{\"safety.motion\": {v}}}")))
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
    let result = reply_rx
        .await
        .map_err(|_| "zmq worker dropped reply".to_string())?;
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
// - motion <1|0 safety master switch; empty = never toggled (defaults on)>
const SYSINFO_SH: &str = concat!(
    "printf 'temp %s\\n' \"$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -rn | head -1)\";",
    "printf 'cpu %s %s\\n' \"$(cut -d' ' -f1 /proc/loadavg)\" \"$(nproc)\";",
    "printf 'gpu %s\\n' \"$(cat /sys/devices/platform/gpu.0/load 2>/dev/null || echo -1)\";",
    "awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print \"mem\",t,a}' /proc/meminfo;",
    "df -m / | awk 'NR==2{print \"disk\",$3,$2}';",
    "for h in /sys/class/hwmon/hwmon*; do if [ \"$(cat $h/name 2>/dev/null)\" = ina3221 ]; then ",
    "printf 'pwr %s %s\\n' \"$(cat $h/in1_input)\" \"$(cat $h/curr1_input)\"; break; fi; done;",
    "printf 'sbatt %s\\n' \"$(cat /tmp/lekiwi_batt 2>/dev/null)\";",
    "printf 'sarm %s\\n' \"$(cat /tmp/lekiwi_arm 2>/dev/null)\";",
    "printf 'motion %s\\n' \"$(cat /tmp/lekiwi_motion 2>/dev/null)\"",
);

/// SSH to the board and read its vitals. BatchMode=yes fails fast (no password
/// prompt) if the key isn't set up, surfacing as an error the UI shows offline.
#[tauri::command]
async fn sysinfo(ip: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let out = std::process::Command::new("ssh")
            .args([
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=6",
                "-o",
                "StrictHostKeyChecking=accept-new",
                // reuse one TCP+auth session across the 4 s polls instead of a
                // full handshake per poll
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPath=/tmp/lekiwi-ssh-%r@%h",
                "-o",
                "ControlPersist=60s",
                &format!("jetson@{ip}"),
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

#[tauri::command]
async fn arm_cal_status(ip: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let out = std::process::Command::new("ssh")
            .args([
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=6",
                "-o",
                "StrictHostKeyChecking=accept-new",
                &format!("jetson@{ip}"),
                "cat /tmp/lekiwi_arm_calibration 2>/dev/null || echo idle",
            ])
            .output()
            .map_err(|e| format!("ssh spawn failed: {e}"))?;
        if out.status.success() {
            Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
        } else {
            Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
        }
    })
    .await
    .map_err(|e| format!("arm calibration status task failed: {e}"))?
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
            if action == "is-enabled" {
                " || true"
            } else {
                ""
            }
        );
        let out = if ip == "127.0.0.1" || ip == "localhost" {
            std::process::Command::new("sh").args(["-c", &sc]).output()
        } else {
            std::process::Command::new("ssh")
                .args([
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=6",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    &format!("jetson@{ip}"),
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
    std::fs::read_to_string(token_dir().join("vlm/token"))
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// ONE shared agent for every daemon call. The connection pool lives on the
/// Agent, so building a fresh one per request (as this used to) meant a new TCP
/// handshake for every frame — 30/s from the vision pump, with the matching
/// TIME_WAIT pile-up. Per-request timeouts are set on the request instead.
fn vlm_agent(_secs: u64) -> ureq::Agent {
    static AGENT: std::sync::OnceLock<ureq::Agent> = std::sync::OnceLock::new();
    AGENT
        .get_or_init(|| {
            ureq::AgentBuilder::new()
                .timeout_connect(Duration::from_secs(4))
                .build()
        })
        .clone()
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
        .timeout(Duration::from_secs(secs))
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

/// List installed VLM models (id, disk_mb, usable, active) for the model dropdown.
/// The actual switch goes through the voice-daemon /config vision job, not here.
#[tauri::command]
async fn vlm_models(ip: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || vlm_get_text(&ip, "/models", 6))
        .await
        .map_err(|e| format!("vlm task failed: {e}"))?
}

/// Fetch the latest frame plus its metadata headers (X-Fps measured capture
/// rate, X-Frame-Ts capture wall time). Returns JSON {b64, fps, frame_ts}; the
/// frontend drops b64 into `img.src = "data:image/jpeg;base64,<...>"` and shows
/// the measured fps.
#[tauri::command]
async fn vlm_frame(ip: String, camera: Option<String>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let auth = vlm_auth()?;
        let path = if camera.as_deref() == Some("wrist") {
            "/frame.jpg?camera=wrist"
        } else {
            "/frame.jpg"
        };
        let resp = vlm_agent(6)
            .get(&vlm_url(&ip, path))
            .timeout(Duration::from_secs(6))
            .set("Authorization", &auth)
            .call()
            .map_err(|e| e.to_string())?;
        let fps: f64 = resp
            .header("X-Fps")
            .and_then(|h| h.parse().ok())
            .unwrap_or(0.0);
        let frame_ts: f64 = resp
            .header("X-Frame-Ts")
            .and_then(|h| h.parse().ok())
            .unwrap_or(0.0);
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
async fn vlm_describe(
    ip: String,
    prompt: String,
    camera: Option<String>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let auth = vlm_auth()?;
        let mut body = serde_json::Map::new();
        if !prompt.trim().is_empty() {
            body.insert("prompt".into(), serde_json::json!(prompt));
        }
        if camera.as_deref() == Some("wrist") {
            body.insert("camera".into(), serde_json::json!("wrist"));
        }
        let body = serde_json::Value::Object(body).to_string();
        vlm_agent(90)
            .post(&vlm_url(&ip, "/describe"))
            .timeout(Duration::from_secs(90))
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
async fn vlm_set_state(
    ip: String,
    state: Option<String>,
    interval: Option<f64>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let auth = vlm_auth()?;
        // Both fields optional: state alone starts/stops 解读, interval alone
        // retunes the cadence without disturbing the current state.
        let mut b = serde_json::Map::new();
        if let Some(s) = state {
            b.insert("state".into(), serde_json::Value::from(s));
        }
        if let Some(i) = interval {
            b.insert("interval".into(), serde_json::Value::from(i));
        }
        let body = serde_json::Value::Object(b).to_string();
        vlm_agent(5)
            .post(&vlm_url(&ip, "/state"))
            .timeout(Duration::from_secs(5))
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

// ---------------------------------------------------------------------------
// voice-daemon proxy — same token-in-Rust pattern as the VLM block above.
// The voice daemon (voice/daemon.py) serves HTTP on tcp://<ip>:8092; the GUI
// polls /health + /feed?since=<seq> and posts /listen /stop /interrupt /say.
const VOICE_PORT: u16 = 8092;

fn voice_auth() -> Result<String, String> {
    std::fs::read_to_string(token_dir().join("voice/token"))
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .map(|t| format!("Bearer {t}"))
        .ok_or_else(|| "no voice token (daemon not running?)".to_string())
}

/// The daemon answers 4xx/5xx with a {"error": "why"} body; surface that reason
/// instead of ureq's bare "status code NNN" (which hides e.g. 先停对话再切大脑).
fn voice_err(e: ureq::Error) -> String {
    match e {
        ureq::Error::Status(code, resp) => {
            let body = resp.into_string().unwrap_or_default();
            serde_json::from_str::<serde_json::Value>(&body)
                .ok()
                .and_then(|v| v.get("error")?.as_str().map(str::to_string))
                .unwrap_or_else(|| {
                    if body.trim().is_empty() {
                        format!("status code {code}")
                    } else {
                        body
                    }
                })
        }
        other => other.to_string(),
    }
}

/// Generic GET proxy: path is fixed by the frontend (health/feed/state only).
#[tauri::command]
async fn voice_get(ip: String, path: String) -> Result<String, String> {
    if !(path == "/health"
        || path == "/state"
        || path.starts_with("/feed")
        || path == "/config"
        || path.starts_with("/asr_debug/tail")
        || path.starts_with("/asr_debug/seg"))
    {
        return Err(format!("bad path: {path}"));
    }
    tauri::async_runtime::spawn_blocking(move || {
        let auth = voice_auth()?;
        vlm_agent(6)
            .get(&format!("http://{ip}:{VOICE_PORT}{path}"))
            .timeout(Duration::from_secs(6))
            .set("Authorization", &auth)
            .call()
            .map_err(voice_err)?
            .into_string()
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| format!("voice task failed: {e}"))?
}

/// Generic POST proxy for the control endpoints. body is a JSON string ("{}"
/// for none); the daemon validates it, we just forward.
#[tauri::command]
async fn voice_post(ip: String, path: String, body: String) -> Result<String, String> {
    if !matches!(
        path.as_str(),
        "/listen"
            | "/stop"
            | "/interrupt"
            | "/say"
            | "/simulate"
            | "/config"
            | "/brain"
            | "/reset"
            | "/asr_debug"
            | "/asr_debug/seg_play"
            | "/asr_debug/seg_asr"
            | "/selftest"
    ) {
        return Err(format!("bad path: {path}"));
    }
    tauri::async_runtime::spawn_blocking(move || {
        let auth = voice_auth()?;
        vlm_agent(15)
            .post(&format!("http://{ip}:{VOICE_PORT}{path}"))
            .timeout(Duration::from_secs(15))
            .set("Authorization", &auth)
            .set("Content-Type", "application/json")
            .send_string(if body.trim().is_empty() { "{}" } else { &body })
            .map_err(voice_err)?
            .into_string()
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| format!("voice task failed: {e}"))?
}

/// Manual control of the voice service, mirroring vlm_service (runtime-only
/// start/stop/restart + boot-autostart enable/disable/is-enabled).
#[tauri::command]
async fn voice_service(ip: String, action: String) -> Result<String, String> {
    if !matches!(
        action.as_str(),
        "start" | "stop" | "restart" | "enable" | "disable" | "is-enabled"
    ) {
        return Err(format!("bad action: {action}"));
    }
    tauri::async_runtime::spawn_blocking(move || {
        let sc = format!(
            "systemctl --user {action} voice-daemon.service{}",
            if action == "is-enabled" {
                " || true"
            } else {
                ""
            }
        );
        let out = if ip == "127.0.0.1" || ip == "localhost" {
            std::process::Command::new("sh").args(["-c", &sc]).output()
        } else {
            std::process::Command::new("ssh")
                .args([
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=6",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    &format!("jetson@{ip}"),
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
    .map_err(|e| format!("voice_service task failed: {e}"))?
}

// ---------------------------------------------------------------- mac speaker
// The Voice page's "电脑播报" bench: THIS Mac speaks a script through its own
// speakers while the robot listens. Deliberately does not touch voice-daemon —
// the whole point is to have a sound source the board did not produce (VAD/ASR
// field tuning, echo tests, wake-word drills).
//
// Volume rides in `say`'s inline [[volm x]] command, not the system output
// volume: a global mute/restore pair is one crash away from leaving the user's
// Mac at 20%, and there is no correct place to restore it from.

/// PID of the `say` child currently speaking, so 停止 can cut a long line.
#[derive(Default)]
struct MacSay {
    pid: std::sync::Arc<std::sync::Mutex<Option<u32>>>,
}

/// Installed `say` voices as "name\tlocale" rows, Chinese ones first (this is a
/// Mandarin robot; en_US voices are still listed, just not in the way).
#[tauri::command]
async fn mac_voices() -> Result<Vec<String>, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let out = std::process::Command::new("say")
            .args(["-v", "?"])
            .output()
            .map_err(|e| format!("say unavailable: {e}"))?;
        let text = String::from_utf8_lossy(&out.stdout).into_owned();
        let (mut zh, mut other) = (Vec::new(), Vec::new());
        for line in text.lines() {
            // "Tingting            zh_CN    # 你好！我叫婷婷。" — the name itself
            // can contain spaces and parens ("Eddy (中文（中国大陆）)"), so split
            // at the LAST whitespace run before the locale, not the first.
            let head = line.split('#').next().unwrap_or("").trim_end();
            let Some(cut) = head.rfind(char::is_whitespace) else {
                continue;
            };
            let (name, locale) = (head[..cut].trim(), head[cut..].trim());
            if name.is_empty() || locale.is_empty() {
                continue;
            }
            let row = format!("{name}\t{locale}");
            if locale.starts_with("zh") {
                zh.push(row)
            } else {
                other.push(row)
            }
        }
        zh.append(&mut other);
        Ok(zh)
    })
    .await
    .map_err(|e| format!("mac_voices task failed: {e}"))?
}

/// Speak one line and block until it finishes. voice/rate empty-or-0 = system
/// default. Killed-by-signal is a deliberate 停止, not an error.
#[tauri::command]
async fn mac_say(
    text: String,
    voice: String,
    volume: u8,
    rate: u32,
    state: State<'_, MacSay>,
) -> Result<(), String> {
    let text = text.trim().to_string();
    if text.is_empty() {
        return Ok(());
    }
    let vol = f32::from(volume.min(100)) / 100.0;
    let slot = state.pid.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut cmd = std::process::Command::new("say");
        let voice = voice.trim();
        if !voice.is_empty() {
            cmd.args(["-v", voice]);
        }
        if rate > 0 {
            cmd.args(["-r", &rate.to_string()]);
        }
        // "[[" in the payload would be read as another embedded command; break it.
        cmd.arg(format!("[[volm {vol:.3}]]{}", text.replace("[[", "[ [")));
        let mut child = cmd.spawn().map_err(|e| format!("say failed: {e}"))?;
        *slot.lock().unwrap() = Some(child.id());
        let st = child.wait();
        *slot.lock().unwrap() = None;
        match st {
            // code() is None when a signal ended it — that is our own 停止.
            Ok(s) if s.success() || s.code().is_none() => Ok(()),
            Ok(s) => Err(format!("say exited {s}")),
            Err(e) => Err(e.to_string()),
        }
    })
    .await
    .map_err(|e| format!("mac_say task failed: {e}"))?
}

/// Cut whatever is speaking right now (`say`, `afplay`, or a renderer). No-op
/// when nothing is running.
#[tauri::command]
fn mac_say_stop(state: State<'_, MacSay>) -> Result<(), String> {
    let pid = *state.pid.lock().unwrap();
    if let Some(p) = pid {
        let _ = std::process::Command::new("kill")
            .arg(p.to_string())
            .status();
    }
    Ok(())
}

/// Render a whole script to files in one helper process (see
/// scripts/mac_tts_render.py for why it is not one call per line). `items` is
/// [{text, out}] with `out` already cache-keyed by the frontend; the helper only
/// fills the missing ones, so editing one line re-renders one line.
#[tauri::command]
async fn mac_tts_render(
    engine: String,
    voice: String,
    seed: u32,
    items: serde_json::Value,
    state: State<'_, MacSay>,
) -> Result<String, String> {
    // Each engine pulls a different dependency; declaring both would make the
    // network-only path drag in a 1.4GB model resolver.
    // f5 needs edge-tts too: its "voice" is a reference clip, and the reference is
    // minted with edge on first use. numpy is for the silence trim.
    let deps = match engine.as_str() {
        "edge" => "--with edge-tts --with numpy",
        "f5" => "--with f5-tts-mlx --with edge-tts --with numpy",
        other => return Err(format!("bad engine: {other}")),
    };
    let script = token_dir().join("scripts/mac_tts_render.py");
    if !script.exists() {
        return Err(format!("找不到 {}", script.display()));
    }
    let cache = tts_cache_dir();
    std::fs::create_dir_all(&cache).map_err(|e| format!("cache dir: {e}"))?;
    // The frontend sends [{text, key}]; the cache path is derived here so only
    // one side owns the layout, and the paths go back so it can play them.
    let src = items.as_array().ok_or("items must be an array")?;
    let mut job_items = Vec::with_capacity(src.len());
    let mut paths = Vec::with_capacity(src.len());
    for it in src {
        let text = it.get("text").and_then(|v| v.as_str()).unwrap_or("");
        let key = it.get("key").and_then(|v| v.as_str()).unwrap_or("");
        if text.is_empty() || key.is_empty() || !key.chars().all(|c| c.is_ascii_alphanumeric()) {
            return Err(format!("bad item: {it}"));
        }
        let out = cache.join(format!("{key}.wav"));
        paths.push(out.to_string_lossy().to_string());
        job_items.push(serde_json::json!({"text": text, "out": out.to_string_lossy()}));
    }
    let job = serde_json::json!({
        "engine": engine, "voice": voice, "seed": seed,
        "cache_dir": cache.to_string_lossy(), "items": job_items,
    })
    .to_string();
    let slot = state.pid.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut child = std::process::Command::new("sh")
            .args([
                "-lc",
                &sh_env(&format!(
                    "exec uv run --quiet {deps} '{}'",
                    script.display()
                )),
            ])
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .map_err(|e| format!("spawn failed: {e}"))?;
        *slot.lock().unwrap() = Some(child.id());
        child
            .stdin
            .take()
            .unwrap()
            .write_all(job.as_bytes())
            .map_err(|e| format!("write job: {e}"))?;
        let out = child.wait_with_output().map_err(|e| e.to_string())?;
        *slot.lock().unwrap() = None;
        if !out.status.success() {
            // The helper's traceback is the only useful thing here; keep the tail.
            let err = String::from_utf8_lossy(&out.stderr);
            return Err(err.lines().rev().take(3).collect::<Vec<_>>().join(" / "));
        }
        let mut res: serde_json::Value =
            serde_json::from_str(String::from_utf8_lossy(&out.stdout).trim())
                .unwrap_or_else(|_| serde_json::json!({"ok": true}));
        res["paths"] = serde_json::json!(paths);
        Ok(res.to_string())
    })
    .await
    .map_err(|e| format!("mac_tts_render task failed: {e}"))?
}

/// Where rendered lines and f5 reference clips live. Keyed by content upstream,
/// so this is a pure cache — deleting it costs a re-render, nothing else.
fn tts_cache_dir() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    std::path::PathBuf::from(home).join(".cache/lekiwi-console/tts")
}

/// Play one rendered file and block until it finishes. `volume` is a percentage;
/// afplay's -v is a linear gain where 1.0 is unity.
#[tauri::command]
async fn mac_play(path: String, volume: u8, state: State<'_, MacSay>) -> Result<(), String> {
    if !std::path::Path::new(&path).exists() {
        return Err(format!("找不到音频 {path}"));
    }
    let vol = f32::from(volume.min(100)) / 100.0;
    let slot = state.pid.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut child = std::process::Command::new("afplay")
            .args(["-v", &format!("{vol:.3}"), &path])
            .spawn()
            .map_err(|e| format!("afplay failed: {e}"))?;
        *slot.lock().unwrap() = Some(child.id());
        let st = child.wait();
        *slot.lock().unwrap() = None;
        match st {
            Ok(s) if s.success() || s.code().is_none() => Ok(()), // signal = 停止
            Ok(s) => Err(format!("afplay exited {s}")),
            Err(e) => Err(e.to_string()),
        }
    })
    .await
    .map_err(|e| format!("mac_play task failed: {e}"))?
}

/// macOS output volume, 0-100. It multiplies `afplay -v`, so a bench sitting at
/// 100% can still be 6 dB down without anything in the GUI saying so — read it
/// out instead of letting the operator guess.
#[tauri::command]
async fn mac_sysvol_get() -> Result<u8, String> {
    let out = std::process::Command::new("osascript")
        .args(["-e", "output volume of (get volume settings)"])
        .output()
        .map_err(|e| format!("osascript failed: {e}"))?;
    String::from_utf8_lossy(&out.stdout)
        .trim()
        .parse::<i32>()
        .map(|v| v.clamp(0, 100) as u8)
        .map_err(|_| "读不到系统音量".to_string())
}

/// Set the macOS output volume. Only ever called from the explicit 拉满 button —
/// changing a machine-wide setting behind the operator's back is not ours to do.
#[tauri::command]
async fn mac_sysvol_set(volume: u8) -> Result<u8, String> {
    let v = volume.min(100);
    std::process::Command::new("osascript")
        .args(["-e", &format!("set volume output volume {v}")])
        .status()
        .map_err(|e| format!("osascript failed: {e}"))?;
    mac_sysvol_get().await
}

// ------------------------------------------------------------- mac ASR server
// "电脑 ASR": a recognizer far bigger than the board can hold, running on THIS
// Mac and reached over the LAN (voice/voice_engines.py RemoteAsr ->
// scripts/mac_asr_server.py). The GUI happens to run on the same Mac, so it owns
// the weight download and the process lifecycle — otherwise every use of the
// feature starts with "open a terminal".
//
// Every shell-out goes through `sh -lc` on purpose: a GUI app inherits launchd's
// minimal PATH, which has neither `uv` nor `hf` in it. A login shell reads the
// user's profile and finds them where they actually are.

const MAC_ASR_PORT: u16 = 8094;

#[derive(Default)]
struct MacAsr {
    child: std::sync::Arc<std::sync::Mutex<Option<std::process::Child>>>,
}

/// Run a shell snippet with the tool dirs actually on PATH.
///
/// `sh -lc` alone is NOT enough and this was measured, not assumed: with a clean
/// environment (what launchd hands a .app) a login sh reads /etc/profile +
/// ~/.profile and ends up without ~/.local/bin, so `uv` and `hf` are both
/// missing. The user's PATH lives in ~/.zshrc, which a login *sh* never reads.
/// So name the standard install dirs explicitly and keep the login shell for
/// everything else it does give us.
fn sh_env(script: &str) -> String {
    format!(
        "export PATH=\"$HOME/.local/bin:$HOME/.homebrew/bin:/opt/homebrew/bin:\
         /usr/local/bin:$PATH\"; {script}"
    )
}

fn login_sh(script: &str) -> std::io::Result<std::process::Output> {
    std::process::Command::new("sh")
        .args(["-lc", &sh_env(script)])
        .output()
}

/// Bytes already on disk for a HF repo id, in the normal hub cache.
fn hf_cached_bytes(repo: &str) -> u64 {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    let hub = std::env::var("HF_HUB_CACHE")
        .or_else(|_| std::env::var("HF_HOME").map(|h| format!("{h}/hub")))
        .unwrap_or_else(|_| format!("{home}/.cache/huggingface/hub"));
    let dir = format!("{hub}/models--{}", repo.replace('/', "--"));
    fn walk(p: &std::path::Path) -> u64 {
        let Ok(rd) = std::fs::read_dir(p) else {
            return 0;
        };
        rd.flatten()
            .map(|e| match e.file_type() {
                // symlinks are the hub's norm (snapshots -> blobs); follow them
                // for size or every cached repo reads as 0 bytes.
                Ok(t) if t.is_dir() => walk(&e.path()),
                _ => std::fs::metadata(e.path()).map(|m| m.len()).unwrap_or(0),
            })
            .sum()
    }
    walk(std::path::Path::new(&dir))
}

/// {cached_mb, running, port, lan_ip} — everything the GUI needs to paint the
/// 电脑 ASR block without guessing.
#[tauri::command]
async fn mac_asr_status(model: String, state: State<'_, MacAsr>) -> Result<String, String> {
    let mine = {
        let mut slot = state.child.lock().unwrap();
        match slot.as_mut().map(|c| c.try_wait()) {
            Some(Ok(None)) => true, // still running
            Some(_) => {
                *slot = None;
                false
            }
            None => false,
        }
    };
    tauri::async_runtime::spawn_blocking(move || {
        // A server someone started in a terminal counts as running too — the port
        // is the truth, our child handle is only how we can stop it.
        let listening = std::net::TcpStream::connect_timeout(
            &std::net::SocketAddr::from(([127, 0, 0, 1], MAC_ASR_PORT)),
            Duration::from_millis(300),
        )
        .is_ok();
        let lan_ip = login_sh("ipconfig getifaddr en0 || ipconfig getifaddr en1")
            .ok()
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .filter(|s| !s.is_empty());
        Ok(serde_json::json!({
            "cached_mb": hf_cached_bytes(&model) / (1024 * 1024),
            "running": listening,
            "owned": mine,
            "port": MAC_ASR_PORT,
            "lan_ip": lan_ip,
        })
        .to_string())
    })
    .await
    .map_err(|e| format!("mac_asr_status task failed: {e}"))?
}

/// Fetch the weights into the normal Hugging Face cache. Blocking on purpose:
/// the frontend disables the button and shows 下载中…, and a fake progress bar
/// would be worse than an honest wait.
#[tauri::command]
async fn mac_asr_download(model: String) -> Result<String, String> {
    if model.trim().is_empty() || model.contains('\'') {
        return Err(format!("bad model id: {model}"));
    }
    tauri::async_runtime::spawn_blocking(move || {
        let out = login_sh(&format!("hf download '{model}'"))
            .map_err(|e| format!("spawn failed: {e}"))?;
        if out.status.success() {
            Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
        } else {
            let err = String::from_utf8_lossy(&out.stderr);
            Err(err.lines().rev().take(3).collect::<Vec<_>>().join(" / "))
        }
    })
    .await
    .map_err(|e| format!("mac_asr_download task failed: {e}"))?
}

/// start | stop the local ASR server. start is detached-but-owned: we keep the
/// Child so stop actually works, and uv resolves the script's PEP 723 deps.
#[tauri::command]
async fn mac_asr_serve(
    action: String,
    model: String,
    state: State<'_, MacAsr>,
) -> Result<String, String> {
    let slot = state.child.clone();
    match action.as_str() {
        "stop" => {
            let mut g = slot.lock().unwrap();
            if let Some(mut c) = g.take() {
                let _ = c.kill();
                let _ = c.wait();
                return Ok("stopped".into());
            }
            // Not ours (started from a terminal) — say so instead of lying.
            Err("本进程没有启动它,请在启动它的终端里停止".into())
        }
        "start" => {
            if model.trim().is_empty() || model.contains('\'') {
                return Err(format!("bad model id: {model}"));
            }
            {
                let mut g = slot.lock().unwrap();
                if let Some(c) = g.as_mut() {
                    if matches!(c.try_wait(), Ok(None)) {
                        return Ok("already running".into());
                    }
                    *g = None;
                }
            }
            let repo = token_dir();
            let script = repo.join("scripts/mac_asr_server.py");
            if !script.exists() {
                return Err(format!("找不到 {}", script.display()));
            }
            let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
            let log = format!("{home}/.config/lekiwi-console/mac-asr.log");
            let cmd = format!(
                "cd '{}' && exec uv run --quiet scripts/mac_asr_server.py \
                 --port {MAC_ASR_PORT} --model '{model}' >> '{log}' 2>&1",
                repo.display()
            );
            let child = std::process::Command::new("sh")
                .args(["-lc", &sh_env(&cmd)])
                .spawn()
                .map_err(|e| format!("spawn failed: {e}"))?;
            *slot.lock().unwrap() = Some(child);
            Ok(log)
        }
        other => Err(format!("bad action: {other}")),
    }
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
        .manage(MacSay::default())
        .manage(MacAsr::default())
        .invoke_handler(tauri::generate_handler![
            load_config,
            save_config,
            zmq_connect,
            zmq_send_base,
            zmq_disconnect,
            zmq_status,
            sysinfo,
            leader_connect,
            leader_align,
            leader_calibrate,
            leader_follow,
            leader_disconnect,
            zmq_arm_mid,
            zmq_arm_relax,
            zmq_arm_calibrate,
            zmq_set_motion,
            arm_cal_status,
            log_connect,
            vlm_health,
            vlm_service,
            vlm_frame,
            vlm_caption,
            vlm_models,
            vlm_describe,
            vlm_set_state,
            voice_get,
            voice_post,
            voice_service,
            mac_voices,
            mac_say,
            mac_say_stop,
            mac_tts_render,
            mac_play,
            mac_sysvol_get,
            mac_sysvol_set,
            mac_asr_status,
            mac_asr_download,
            mac_asr_serve,
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

#[cfg(test)]
mod tests {
    use super::{decode_sign_magnitude, encode_sign_magnitude};

    #[test]
    fn sts_sign_magnitude_round_trip() {
        for value in [-2047, -1376, -1, 0, 1, 1376, 2047] {
            let encoded = encode_sign_magnitude(value, 11).unwrap();
            assert_eq!(decode_sign_magnitude(encoded, 11), value);
        }
        assert!(encode_sign_magnitude(2048, 11).is_err());
    }
}
