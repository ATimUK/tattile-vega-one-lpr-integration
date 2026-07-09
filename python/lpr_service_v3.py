#!/usr/bin/env python3
"""
LPR Service v3 — License Plate Recognition service
Tattile Vega One cameras -> fast-alpr ONNX -> DRB server

Processing logic:
  - Camera score >= camera_score_threshold:
      Apply character replacements only, forward packet as-is
  - Camera score  < camera_score_threshold:
      Run fast-alpr on both IMAGE_OCR and IMAGE_CTX,
      pick the result with higher confidence, apply replacements, forward

TCP reliability fixes:
  - End-of-packet detection via </root> closing tag
  - Global watchdog timer for the entire receive operation
  - Handles keep-alive connections (camera does not close socket after send)
  - DRB send with configurable retry
"""

import base64
import csv
import datetime
import json
import logging
import os
import re
import select
import socket
import statistics
import threading
import time
import xml.etree.ElementTree as ET
from typing import Callable, Optional, Tuple

try:
    import cv2
    import numpy as np
    # Suppress "Corrupt JPEG data: N extraneous bytes before marker 0xda"
    # Vega One cameras embed proprietary metadata into the JPEG stream before
    # the SOS marker. OpenCV logs this as a warning but decodes the image
    # correctly. Silencing it keeps the log clean.
    if hasattr(cv2,"setLogLevel"): 
        cv2.setLogLevel(0)
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False

try:
    from fast_alpr import ALPR
    FAST_ALPR_AVAILABLE = True
except ImportError:
    FAST_ALPR_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG: dict = {
    # Incoming TCP server (packets from cameras)
    "listen_host": "0.0.0.0",
    "listen_port": 50003,

    # DRB server (where the modified XML is forwarded)
    "drb_host": "192.168.1.100",
    "drb_port": 50004,
    "drb_retry_count": 3,       # number of send attempts to DRB
    "drb_retry_delay": 2.0,     # seconds between retries

    # Camera score threshold:
    #   BELOW -> run fast-alpr on OCR + CTX images, pick the best result
    #   ABOVE -> apply character replacements only, skip OCR inference
    "camera_score_threshold": 80,

    # fast-alpr ONNX models
    # Detector options: yolo-v9-s-608-license-plate-end2end (default, most accurate)
    #                   yolo-v9-t-640, yolo-v9-t-512, yolo-v9-t-384 (faster, smaller)
    "detector_model": "yolo-v9-s-608-license-plate-end2end",
    "detector_conf_thresh": 0.4,

    # OCR options: cct-s-v2-global-model (default, highest accuracy)
    #              cct-xs-v2-global-model (smaller/faster)
    #              global-plates-mobile-vit-v2-model
    "ocr_model": "cct-s-v2-global-model",

    # Minimum fast-alpr OCR confidence to replace the camera result (0.0-1.0)
    "min_ocr_confidence": 0.50,

    # Character substitutions applied to the final plate string
    # Example: replace letter O with digit 0
    "char_replacements": {
        "O": "0",
        "I": "1",
        "Q": "0"
    },

    # CSV processing log
    "enable_csv_log": True,
    "csv_log_path": "logs/lpr_log.csv",

    # Save decoded images to disk (useful for debugging)
    "enable_image_save": False,
    "image_save_path": "images",

    # Logging level: DEBUG | INFO | WARNING | ERROR
    "log_level": "INFO",

    # TCP receive parameters
    "socket_recv_timeout": 5,       # seconds to wait between chunks
    "socket_total_timeout": 30,     # maximum seconds for the entire receive
    "max_packet_size": 524288,      # 512 KB — Vega One packets are typically < 500 KB
    "max_connections": 20,
}

CONFIG_FILE = "config.json"


def load_config(path: str = CONFIG_FILE) -> dict:
    """Load config from JSON file, filling missing keys from defaults."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        return {**DEFAULT_CONFIG, **user}
    # Create default config file on first run
    with open(path, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=4)
    logging.info(f"Default config created: {path}")
    return DEFAULT_CONFIG.copy()


def setup_logging(level_str: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level_str.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ══════════════════════════════════════════════════════════════════════════════
# fast-alpr SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_alpr_instance = None
_alpr_lock = threading.Lock()


def get_alpr(cfg: dict):
    """Initialize (once) and return the shared ALPR instance."""
    global _alpr_instance
    if not FAST_ALPR_AVAILABLE or not OPENCV_AVAILABLE:
        return None
    with _alpr_lock:
        if _alpr_instance is None:
            det = cfg.get("detector_model", "yolo-v9-s-608-license-plate-end2end")
            ocr = cfg.get("ocr_model", "cct-s-v2-global-model")
            thr = float(cfg.get("detector_conf_thresh", 0.4))
            logging.info(f"Loading fast-alpr: detector={det}, ocr={ocr}")
            _alpr_instance = ALPR(
                detector_model=det,
                detector_conf_thresh=thr,
                ocr_model=ocr,
            )
            logging.info("fast-alpr ready")
    return _alpr_instance


# ══════════════════════════════════════════════════════════════════════════════
# TCP RECEIVE — RELIABLE
# ══════════════════════════════════════════════════════════════════════════════

# End-of-packet marker in Vega One XML stream
XML_END_MARKER = b"</root>"


def recv_packet(sock: socket.socket, cfg: dict) -> bytes:
    """
    Reliably receive one XML packet from a Vega One camera.

    Strategy:
    1. Read chunks via select() (non-blocking) until </root> is found.
    2. If the camera closes the connection (recv returns b""), treat as end.
    3. Global deadline: abort if socket_total_timeout expires before </root>.
    4. Drop oversized packets to protect against memory exhaustion.

    Why not a simple timeout?
    Vega One keeps the TCP connection open (keep-alive) after sending the XML.
    A plain recv() would block forever waiting for more data that never comes.
    Detecting </root> lets us return immediately after the packet is complete.
    """
    recv_timeout  = float(cfg.get("socket_recv_timeout", 5))
    total_timeout = float(cfg.get("socket_total_timeout", 30))
    max_size      = int(cfg.get("max_packet_size", 524288))

    sock.setblocking(False)

    buf      = bytearray()
    deadline = time.monotonic() + total_timeout

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logging.warning(f"Receive timeout ({total_timeout}s) — packet may be incomplete")
            break

        wait = min(recv_timeout, remaining)
        try:
            readable, _, _ = select.select([sock], [], [], wait)
        except Exception as e:
            logging.error(f"select() error: {e}")
            break

        if not readable:
            # No data arrived within recv_timeout seconds
            if XML_END_MARKER in buf:
                # Packet is complete; camera is simply holding the connection open
                break
            # Still waiting for data — keep looping until deadline
            continue

        try:
            chunk = sock.recv(65536)
        except Exception as e:
            logging.error(f"recv() error: {e}")
            break

        if not chunk:
            # Camera closed the connection — end of packet
            break

        buf.extend(chunk)

        if len(buf) > max_size:
            logging.error(
                f"Packet exceeds max_packet_size ({max_size} bytes) — dropping"
            )
            return b""

        # Found the XML closing tag — trim anything after it and return
        if XML_END_MARKER in buf:
            end_idx = buf.index(XML_END_MARKER) + len(XML_END_MARKER)
            buf = buf[:end_idx]
            break

    return bytes(buf)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def decode_b64_image(b64: str) -> Optional["np.ndarray"]:
    """Decode a base64 JPEG string into a BGR numpy array."""
    if not OPENCV_AVAILABLE or not b64:
        return None
    try:
        arr = np.frombuffer(base64.b64decode(b64.strip()), dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception as e:
        logging.error(f"Image decode error: {e}")
        return None


def apply_replacements(text: str, replacements: dict) -> str:
    """Apply character substitution map to a plate string."""
    for src, dst in replacements.items():
        text = text.replace(str(src), str(dst))
    return text


def run_alpr_on_image(img: "np.ndarray", alpr) -> Tuple[str, float]:
    """
    Run fast-alpr on a single image.
    Returns (plate_text, mean_confidence).
    Picks the detection with the highest OCR confidence.
    """
    try:
        results = alpr.predict(img)
    except Exception as e:
        logging.error(f"fast-alpr inference error: {e}")
        return "", 0.0

    best_text, best_conf = "", 0.0
    for r in results:
        if r.ocr is None:
            continue
        text = (r.ocr.text or "").strip()
        c    = r.ocr.confidence
        conf = statistics.mean(c) if isinstance(c, (list, tuple)) else float(c or 0)
        if text and conf > best_conf:
            best_text, best_conf = text, conf

    return best_text, best_conf


# ══════════════════════════════════════════════════════════════════════════════
# CSV LOG
# ══════════════════════════════════════════════════════════════════════════════

CSV_FIELDS = [
    "datetime", "site_address",
    "orig_plate", "orig_score",
    "new_plate",  "new_score",
    "source",          # camera_high_score | ocr_image | ctx_image |
                       # camera_low_score_fallback | camera_no_detection | camera_no_alpr
    "client_ip", "client_port",
]
_csv_lock = threading.Lock()
_packet_observer: Optional[Callable[[bytes, bytes, dict, tuple], None]] = None


def set_packet_observer(observer: Optional[Callable[[bytes, bytes, dict, tuple], None]]) -> None:
    """Register an optional callback called after a packet is processed."""
    global _packet_observer
    _packet_observer = observer


def notify_packet_observer(raw: bytes, modified: bytes, log_row: dict, addr: tuple) -> None:
    if _packet_observer is None:
        return
    try:
        _packet_observer(raw, modified, log_row, addr)
    except Exception:
        logging.exception("Packet observer failed")


def write_csv(cfg: dict, row: dict) -> None:
    if not cfg.get("enable_csv_log"):
        return
    path = cfg["csv_log_path"]
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    new_file = not os.path.exists(path)
    with _csv_lock:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow(row)


def save_image(img: "np.ndarray", label: str, plate: str, site: str, cfg: dict) -> None:
    """Save a decoded plate image to disk for debugging."""
    if not cfg.get("enable_image_save") or not OPENCV_AVAILABLE or img is None:
        return
    try:
        d = cfg.get("image_save_path", "images")
        os.makedirs(d, exist_ok=True)
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = re.sub(r"[^\w\-]", "_", f"{ts}_{site}_{plate}_{label}")
        cv2.imwrite(os.path.join(d, f"{name}.jpg"), img)
    except Exception as e:
        logging.error(f"Image save error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# PACKET PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

def process_packet(raw: bytes, cfg: dict, addr: tuple) -> Tuple[bytes, dict]:
    """
    Parse the XML packet, apply recognition logic, and return the modified XML.

    Case A — camera score >= camera_score_threshold:
        Apply char_replacements to the camera plate string.
        Do NOT run fast-alpr inference.

    Case B — camera score < camera_score_threshold:
        Decode IMAGE_OCR and IMAGE_CTX.
        Run fast-alpr on each image independently.
        Choose the result with the higher OCR confidence.
        If best_conf >= min_ocr_confidence: use that plate + apply replacements.
        Otherwise: fall back to camera plate + apply replacements.

    In both cases PLATE_STRING and OCRSCORE tags are updated in the XML.
    All other tags are forwarded unchanged.
    """
    try:
        xml_text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        logging.error(f"Raw bytes decode error: {e}")
        return raw, {}

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logging.error(f"XML parse error from {addr}: {e}")
        return raw, {}

    def txt(tag: str) -> str:
        el = root.find(tag)
        return (el.text or "").strip() if el is not None else ""

    img_ocr_b64  = txt("IMAGE_OCR")
    img_ctx_b64  = txt("IMAGE_CTX")
    orig_plate   = txt("PLATE_STRING")
    orig_score_s = txt("OCRSCORE")
    site         = txt("SITE_ADDRESS")

    try:
        orig_score = int(orig_score_s)
    except ValueError:
        orig_score = 0

    replacements    = cfg.get("char_replacements", {})
    score_threshold = int(cfg.get("camera_score_threshold", 80))
    min_ocr_conf    = float(cfg.get("min_ocr_confidence", 0.50))

    new_plate = orig_plate
    new_score = orig_score
    source    = "camera_high_score"

    # ── Case A: camera score is high enough ───────────────────────────────
    if orig_score >= score_threshold:
        new_plate = apply_replacements(orig_plate.upper(), replacements)
        source    = "camera_high_score"

        if new_plate != orig_plate:
            logging.info(
                f"[{addr[0]}:{addr[1]}] SITE={site} | "
                f"Score {orig_score} >= {score_threshold} (high). "
                f"Replacements applied: '{orig_plate}' -> '{new_plate}'"
            )
        else:
            logging.info(
                f"[{addr[0]}:{addr[1]}] SITE={site} | "
                f"Score {orig_score} >= {score_threshold} (high). "
                f"No replacements needed: '{orig_plate}'"
            )

    # ── Case B: camera score is low — run fast-alpr ───────────────────────
    else:
        logging.info(
            f"[{addr[0]}:{addr[1]}] SITE={site} | "
            f"Score {orig_score} < {score_threshold} (low) — running OCR"
        )
        alpr = get_alpr(cfg)

        if alpr and OPENCV_AVAILABLE:
            img_ocr = decode_b64_image(img_ocr_b64)
            img_ctx = decode_b64_image(img_ctx_b64)

            candidates = []  # list of (text, conf, label, img)

            if img_ocr is not None:
                t, c = run_alpr_on_image(img_ocr, alpr)
                candidates.append((t, c, "ocr_image", img_ocr))
                logging.info(f"  IMAGE_OCR -> '{t}' conf={c:.2f}")
            else:
                logging.warning("  IMAGE_OCR: could not decode")

            if img_ctx is not None:
                t, c = run_alpr_on_image(img_ctx, alpr)
                candidates.append((t, c, "ctx_image", img_ctx))
                logging.info(f"  IMAGE_CTX -> '{t}' conf={c:.2f}")
            else:
                logging.warning("  IMAGE_CTX: could not decode")

            # Pick the candidate with the highest confidence
            valid = [(t, c, lbl, img) for t, c, lbl, img in candidates if t]

            if valid:
                best_text, best_conf, best_lbl, best_img = max(valid, key=lambda x: x[1])
                best_replaced = apply_replacements(best_text.upper(), replacements)

                if best_conf >= min_ocr_conf:
                    new_plate = best_replaced
                    new_score = int(best_conf * 100)
                    source    = best_lbl
                    logging.info(
                        f"  Best result [{best_lbl}]: "
                        f"'{best_text}' -> '{new_plate}' conf={best_conf:.2f} "
                        f"(score={new_score})"
                    )
                    if cfg.get("enable_image_save"):
                        save_image(best_img, best_lbl, new_plate, site, cfg)
                else:
                    new_plate = apply_replacements(orig_plate.upper(), replacements)
                    source    = "camera_low_score_fallback"
                    logging.info(
                        f"  Best conf={best_conf:.2f} < min={min_ocr_conf:.2f}. "
                        f"Keeping camera plate with replacements: '{new_plate}'"
                    )
            else:
                # fast-alpr found no plates on either image
                new_plate = apply_replacements(orig_plate.upper(), replacements)
                source    = "camera_no_detection"
                logging.warning(
                    f"  fast-alpr detected no plates. "
                    f"Keeping camera plate with replacements: '{new_plate}'"
                )
        else:
            # fast-alpr not available — apply replacements only
            new_plate = apply_replacements(orig_plate.upper(), replacements)
            source    = "camera_no_alpr"
            logging.warning("fast-alpr not available — applying replacements only")

    # ── Update XML tags ───────────────────────────────────────────────────
    for tag, val in [("PLATE_STRING", new_plate), ("OCRSCORE", f"{new_score:03d}")]:
        el = root.find(tag)
        if el is not None:
            el.text = val

    header       = "<?xml version='1.0' encoding='UTF-8' standalone='no'?>\n"
    result_bytes = (header + ET.tostring(root, encoding="unicode")).encode("utf-8")

    log_row = {
        "datetime":     datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "site_address": site,
        "orig_plate":   orig_plate,
        "orig_score":   orig_score,
        "new_plate":    new_plate,
        "new_score":    new_score,
        "source":       source,
        "client_ip":    addr[0],
        "client_port":  addr[1],
    }
    return result_bytes, log_row


# ══════════════════════════════════════════════════════════════════════════════
# SEND TO DRB (with retry)
# ══════════════════════════════════════════════════════════════════════════════

def send_to_drb(data: bytes, cfg: dict) -> bool:
    host        = cfg["drb_host"]
    port        = int(cfg["drb_port"])
    timeout     = float(cfg.get("socket_total_timeout", 30))
    retry_count = int(cfg.get("drb_retry_count", 3))
    retry_delay = float(cfg.get("drb_retry_delay", 2.0))

    for attempt in range(1, retry_count + 1):
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.sendall(data)
            logging.info(f"-> DRB {host}:{port} ({len(data)} bytes)")
            return True
        except Exception as e:
            logging.error(f"DRB {host}:{port} attempt {attempt}/{retry_count}: {e}")
            if attempt < retry_count:
                time.sleep(retry_delay)

    logging.error(f"DRB {host}:{port}: all {retry_count} attempts failed")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# CLIENT HANDLER (per-connection thread)
# ══════════════════════════════════════════════════════════════════════════════

def handle_client(conn: socket.socket, addr: tuple, cfg: dict) -> None:
    logging.info(f"<- Connection from {addr[0]}:{addr[1]}")
    t_start = time.monotonic()
    try:
        raw = recv_packet(conn, cfg)
    except Exception:
        logging.exception(f"Receive error for client {addr}")
        raw = b""
    finally:
        # ← Закрываем соединение с камерой СРАЗУ после получения пакета,
        #   не дожидаясь OCR и отправки в DRB
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        logging.debug(f"Connection closed: {addr[0]}:{addr[1]}")

    if not raw:
        logging.warning(f"Empty or incomplete packet from {addr[0]}:{addr[1]}")
        return

    elapsed = time.monotonic() - t_start
    logging.debug(f"Received {len(raw)} bytes from {addr[0]}:{addr[1]} in {elapsed:.2f}s")

    try:
        modified, log_row = process_packet(raw, cfg, addr)
        if log_row:
            write_csv(cfg, log_row)
        if modified:
            notify_packet_observer(raw, modified, log_row, addr)
            send_to_drb(modified, cfg)
    except Exception:
        logging.exception(f"Unhandled error processing packet from {addr}")


# ══════════════════════════════════════════════════════════════════════════════
# TCP SERVER
# ══════════════════════════════════════════════════════════════════════════════

def run_server(cfg: dict, stop_event: Optional[threading.Event] = None) -> None:
    host     = cfg["listen_host"]
    port     = int(cfg["listen_port"])
    max_conn = int(cfg.get("max_connections", 20))

    # Warm up ONNX models at startup to avoid latency on first packet
    if FAST_ALPR_AVAILABLE and OPENCV_AVAILABLE:
        logging.info("Loading ONNX models (first run downloads from HuggingFace)...")
        get_alpr(cfg)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    srv.bind((host, port))
    srv.listen(max_conn)
    srv.settimeout(1.0)

    thr = int(cfg.get("camera_score_threshold", 80))
    logging.info("=" * 65)
    logging.info("  LPR Service v3 (fast-alpr ONNX)")
    logging.info(f"  Listen          : {host}:{port}")
    logging.info(f"  DRB server      : {cfg['drb_host']}:{cfg['drb_port']}")
    logging.info(f"  Detector model  : {cfg['detector_model']}")
    logging.info(f"  OCR model       : {cfg['ocr_model']}")
    logging.info(f"  Score threshold : {thr}  (below -> OCR, above -> replacements only)")
    logging.info(f"  Min OCR conf    : {cfg['min_ocr_confidence']}")
    logging.info(f"  Replacements    : {cfg.get('char_replacements', {})}")
    logging.info(f"  Max packet size : {cfg['max_packet_size']} bytes")
    logging.info(f"  CSV log         : {'ON -> ' + cfg['csv_log_path'] if cfg.get('enable_csv_log') else 'OFF'}")
    logging.info(f"  Save images     : {'ON -> ' + cfg['image_save_path'] if cfg.get('enable_image_save') else 'OFF'}")
    logging.info(f"  Recv timeouts   : chunk={cfg['socket_recv_timeout']}s  total={cfg['socket_total_timeout']}s")
    logging.info(f"  fast-alpr       : {'available' if FAST_ALPR_AVAILABLE else 'NOT installed'}")
    logging.info(f"  OpenCV          : {'available' if OPENCV_AVAILABLE else 'NOT installed'}")
    logging.info("=" * 65)

    try:
        while stop_event is None or not stop_event.is_set():
            try:
                conn, addr = srv.accept()
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except socket.timeout:
                continue
            except Exception as e:
                logging.error(f"accept() error: {e}")
                continue
            threading.Thread(
                target=handle_client,
                args=(conn, addr, cfg),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        logging.info("Server stopped (Ctrl+C).")
    finally:
        srv.close()
        logging.info("Server stopped.")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cfg = load_config()
    setup_logging(cfg.get("log_level", "INFO"))
    run_server(cfg)
