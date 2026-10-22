"""Own one broker until the VCP control pipe closes, including after SIGKILL."""
import os
import selectors
import signal
import subprocess
import sys


def supervise(lock_fd, executable, config):
    # The inherited flock spans spawn through child reaping. A replacement VCP
    # cannot start another broker while this process is cleaning up the old one.
    child = None
    try:
        child = subprocess.Popen([executable, '-c', config], stdin=subprocess.DEVNULL)
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin, selectors.EVENT_READ)
            while child.poll() is None:
                if not selector.select(.1):
                    continue
                command = os.read(sys.stdin.fileno(), 4096)
                if not command:
                    break
                for _ in range(command.count(b'R')):
                    if child.poll() is None:
                        child.send_signal(signal.SIGHUP)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        os.close(lock_fd)


if __name__ == '__main__':
    supervise(int(sys.argv[1]), sys.argv[2], sys.argv[3])
