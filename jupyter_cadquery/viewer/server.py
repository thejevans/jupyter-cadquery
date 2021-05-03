from datetime import datetime
from time import localtime
import os
import pickle
import threading
import time
import zmq

from IPython.display import display, clear_output
import ipywidgets as widgets
from jupyter_cadquery.cad_display import CadqueryDisplay
from jupyter_cadquery.defaults import split_args

CAD_DISPLAY = None
LOG_OUTPUT = None
ZMQ_SERVER = None
ZMQ_PORT = 5555


def log(typ, *msg):
    ts = datetime(*localtime()[:6]).isoformat()
    prefix = f"{ts} ({typ}) "
    if LOG_OUTPUT is not None:
        if isinstance(msg, (tuple, list)):
            LOG_OUTPUT.append_stdout(prefix + " ".join([str(m) for m in msg]) + "\n")
        else:
            LOG_OUTPUT.append_stdout(prefix + str(msg) + "\n")
    else:
        print(prefix, *msg)


def info(*msg):
    log("I", *msg)


def warn(*msg):
    log("W", *msg)


def error(*msg):
    log("E", *msg)


def recv_pickle(socket, flags=0, protocol=4):
    p = socket.recv(flags)
    try:
        data = pickle.loads(p)
        return data
    except Exception as ex:
        return str(ex)


def stop_viewer():
    global ZMQ_SERVER

    if ZMQ_SERVER is not None:
        try:
            ZMQ_SERVER.close()
            info("zmq stopped")
            if CAD_DISPLAY is not None and CAD_DISPLAY.info is not None:
                CAD_DISPLAY.info.add_html("<b>HTTP zmq stopped</b>")
            ZMQ_SERVER = None
            time.sleep(0.5)
        except Exception as ex:
            error("Exception %s" % ex)


def start_viewer():
    global CAD_DISPLAY, LOG_OUTPUT, ZMQ_SERVER, ZMQ_PORT

    CAD_DISPLAY = CadqueryDisplay()
    cad_view = CAD_DISPLAY.create()
    width = CAD_DISPLAY.cad_width + CAD_DISPLAY.tree_width + 6
    LOG_OUTPUT = widgets.Output(layout=widgets.Layout(height="400px", overflow="scroll"))

    clear_output()
    log_view = widgets.Accordion(children=[LOG_OUTPUT], layout=widgets.Layout(width=f"{width}px"))
    log_view.set_title(0, "Log")
    log_view.selected_index = None
    display(widgets.VBox([cad_view, log_view]))

    stop_viewer()

    if os.environ.get("ZMQ_PORT") != None:
        ZMQ_PORT = os.environ.get("ZMQ_PORT")
        info(f"Using port {ZMQ_PORT}")

    context = zmq.Context()
    socket = context.socket(zmq.PAIR)
    for i in range(3):
        try:
            socket.bind(f"tcp://*:{ZMQ_PORT}")
            time.sleep(1)
            break
        except:
            pass

    ZMQ_SERVER = socket
    info("zmq started\n")

    def msg_handler():
        while True:
            msg = recv_pickle(socket)
            try:
                if msg.get("type") == "data":
                    t = time.time()
                    data = msg["data"]
                    config = msg["config"]
                    info(config)
                    create_args, add_shape_args = split_args(config)
                    CAD_DISPLAY._update_settings(**create_args)
                    CAD_DISPLAY.add_shapes(**data, **add_shape_args)
                    info(f"duration: {time.time() - t:7.2f}")
                else:
                    error(f"Wrong message type {msg.get('type')}")

            except Exception as ex:
                error(ex)

    thread = threading.Thread(target=msg_handler)
    thread.setDaemon(True)
    thread.start()
    CAD_DISPLAY.info.add_html("<b>zmq server started</b>")
