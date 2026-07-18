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

use std::time::Duration;

use tauri::async_runtime::Mutex;
use tauri::{Manager, State};
use tokio::sync::{mpsc, oneshot};
use zeromq::{PushSocket, Socket, SocketSend, ZmqMessage};

/// Messages the frontend commands hand to the ZMQ worker thread.
enum Req {
    Connect(String, oneshot::Sender<Result<String, String>>),
    Send(f64, f64, f64),
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
        ])
        .setup(|app| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.set_focus();
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
