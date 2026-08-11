import hashlib
import os
import time
import traceback

from PySide6 import QtCore

from Lib.bd_client import BDClient
from utils import version

HOST = "www.majjcom.site"
PORT = 11289
NO_UPDATE = True  # 测试时请改为 False


class UpdateChecker(QtCore.QThread):
    find_update = QtCore.Signal(str, str)

    def __init__(self, parent: QtCore.QObject = None) -> None:
        super().__init__(parent)

    def run(self) -> None:
        if NO_UPDATE:
            return
        c = None
        try:
            c = BDClient(HOST, PORT)
            get = c.request({
                "act": "ver",
                "ver": version.__version__
            })
            new_ver = get.get("ver")
            if not new_ver or not version.check_update(new_ver):
                return

            # 获取更新详情
            c.close()
            c = BDClient(HOST, PORT)
            get = c.request({
                "act": "info",
                "ver": version.__version__
            })
            info = get.get("data", "")
            self.find_update.emit(new_ver, info)
        except Exception as e:
            print(f"[UpdateChecker] Error: {e}")
        finally:
            if c:
                try:
                    c.close()
                except Exception:
                    pass


class UpdateDownloader(QtCore.QThread):
    update_process = QtCore.Signal(int, int)
    download_install = QtCore.Signal(str)
    download_err = QtCore.Signal(str)

    def __init__(self, parent: QtCore.QObject = None) -> None:
        super().__init__(parent)
        self.dir_path = None
        self.url = None
        self.file_hash = None
        self.hash_type = None
        self.file_name = None
        self.save_path = None
        self.size = 0
        self.total = 0
        self._stop_flag = False

    def setup(self, path: str):
        self.dir_path = path
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def _verify_file_hash(self, file_path: str) -> bool:
        """验证本地文件的哈希值"""
        if not os.path.exists(file_path):
            return False
        try:
            hasher = hashlib.sha256 if self.hash_type == "sha256" else hashlib.md5
            h = hasher()
            with open(file_path, "rb") as f:
                while True:
                    data = f.read(8192)
                    if not data:
                        break
                    h.update(data)
            return h.hexdigest().lower() == self.file_hash.lower()
        except Exception:
            return False

    def run(self):
        client = None
        try:
            # 1. 获取下载信息
            client = BDClient(HOST, PORT)
            get = client.request({"act": "url", "ver": version.__version__})
            client.close()
            client = None

            self.url = get["url"]
            self.file_hash = get["hash"]
            self.hash_type = get.get("hash_type", "md5")
            self.file_name = get["name"]
            self.save_path = os.path.join(self.dir_path, self.file_name)

            # 2. 检查本地是否已有完整文件
            if self._verify_file_hash(self.save_path):
                self.update_process.emit(100, 100)
                self.download_install.emit(self.save_path)
                return

            # 3. 开始下载
            self.size = 0
            self.total = 0
            hasher = hashlib.sha256 if self.hash_type == "sha256" else hashlib.md5
            builder = hasher()

            from urllib.request import Request, urlopen
            req = Request(url=self.url, method="GET")
            req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

            with urlopen(req) as resp:
                content_length = resp.headers.get("content-length")
                self.total = int(content_length) if content_length else 0

                with open(self.save_path, "wb") as f:
                    while not self._stop_flag:
                        buffer = resp.read(8192)
                        if not buffer:
                            break
                        f.write(buffer)
                        builder.update(buffer)
                        self.size += len(buffer)
                        # 每下载一点就发射一次进度信号
                        self.update_process.emit(self.size, self.total)

            if self._stop_flag:
                return

            # 4. 校验下载完成的文件
            md5 = builder.hexdigest().lower()
            if md5 != self.file_hash.lower():
                raise Exception("下载哈希无法对应，文件可能已损坏")

            self.download_install.emit(self.save_path)

        except Exception as e:
            self.download_err.emit(str(e) + "\n" + traceback.format_exc())
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass
