import hashlib
import os
import time
import traceback
from urllib.request import Request, urlopen

from PySide6 import QtCore

from Lib.bd_client import BDClient
from utils import version

# ================= 配置区 =================
HOST = "www.majjcom.site"
PORT = 11289
# 设置为 True 可跳过更新检查（用于开发调试或打包便携版）
NO_UPDATE = False 
# ==========================================


class UpdateChecker(QtCore.QThread):
    """
    检查更新线程
    信号: 
        find_update(new_version: str, info: str) - 发现新版本时触发
        (注: finished 信号继承自 QThread，线程结束时自动触发)
    """
    find_update = QtCore.Signal(str, str)

    def __init__(self, parent: QtCore.QObject = None) -> None:
        super().__init__(parent)

    def run(self) -> None:
        if NO_UPDATE:
            return
        
        client = None
        try:
            client = BDClient(HOST, PORT)
            
            # 1. 检查是否有新版本
            resp_ver = client.request({"act": "ver", "ver": version.__version__})
            new_ver = resp_ver.get("ver")
            
            # 如果没有返回新版本，或者新版本不高于当前版本，则退出
            if not new_ver or not version.check_update(new_ver):
                return

            # 2. 获取更新日志/信息
            resp_info = client.request({"act": "info", "ver": version.__version__})
            info_data = resp_info.get("data", "发现新版本，建议更新")
            
            # 3. 通知主窗口
            self.find_update.emit(new_ver, info_data)

        except Exception as e:
            print(f"[UpdateChecker] Error: {e}")
        finally:
            if client:
                try: 
                    client.close()
                except: 
                    pass


class UpdateDownloader(QtCore.QThread):
    """
    下载更新线程
    信号: 
        update_process(current: int, total: int) - 下载进度
        download_install(file_path: str) - 下载完成，准备安装
        download_err(error_msg: str) - 下载出错
    """
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
        self.timer = None
        self.timer_finished = False

    def setup(self, path: str):
        """配置下载目录"""
        self.dir_path = path
        self.timer_finished = False
        self.size = 0
        self.total = 0

    @QtCore.Slot()
    def timer_timeout(self):
        """定时器回调，用于更新 UI 进度"""
        if self.total > 0:
            self.update_process.emit(self.size, self.total)
        
        # 如果下载已完成，停止定时器
        if self.total > 0 and self.size >= self.total:
            self.stop_timer()

    def stop_timer(self):
        """安全停止并清理定时器"""
        if self.timer and self.timer.isActive():
            self.timer.stop()
            self.timer.deleteLater()
            self.timer = None
        self.timer_finished = True

    def run(self):
        client = None
        try:
            # 1. 从服务器获取下载链接和文件信息
            client = BDClient(HOST, PORT)
            get_data = client.request({"act": "url", "ver": version.__version__})
            
            self.url = get_data["url"]
            self.file_hash = get_data.get("hash", "")
            self.hash_type = get_data.get("hash_type", "md5").lower()
            self.file_name = get_data["name"]
            self.save_path = os.path.join(self.dir_path, self.file_name)
            
            # 根据服务器返回选择哈希算法
            hasher_cls = hashlib.sha256 if self.hash_type == "sha256" else hashlib.md5

            # 2. 检查本地是否已有正确文件（断点续传/避免重复下载）
            if os.path.exists(self.save_path) and self.file_hash:
                if self._verify_file_hash(self.save_path, self.file_hash, hasher_cls):
                    self.update_process.emit(100, 100)
                    self.download_install.emit(self.save_path)
                    return
                else:
                    # 文件损坏，删除旧文件
                    try: 
                        os.remove(self.save_path)
                    except: 
                        pass

            # 3. 启动进度定时器
            self.timer = QtCore.QTimer()
            self.timer.setInterval(200)  # 200ms 刷新一次，减轻 UI 压力
            self.timer.timeout.connect(self.timer_timeout)
            self.timer.start()

            # 4. 开始 HTTP 下载
            builder = hasher_cls()
            req = Request(url=self.url, method="GET")
            req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')

            with urlopen(req) as resp:
                content_length = resp.headers.get("content-length")
                if content_length:
                    self.total = int(content_length)
                
                with open(self.save_path, "wb") as f:
                    while True:
                        buffer = resp.read(8192)  # 8KB 缓冲区，提升 I/O 效率
                        if not buffer: 
                            break
                        f.write(buffer)
                        builder.update(buffer)
                        self.size += len(buffer)

            # 等待最后一次定时器触发，确保进度条走到 100%
            while not self.timer_finished:
                time.sleep(0.05)
                
            # 5. 校验文件哈希
            if self.file_hash and builder.hexdigest().lower() != self.file_hash.lower():
                raise Exception("文件哈希校验失败，下载的文件可能已损坏")
            
            # 6. 通知主窗口下载完成
            self.download_install.emit(self.save_path)

        except Exception as e:
            # 发送详细错误信息，包含堆栈跟踪方便调试
            err_msg = f"{str(e)}\n{traceback.format_exc()}"
            self.download_err.emit(err_msg)
        finally:
            self.stop_timer()
            if client:
                try: 
                    client.close()
                except: 
                    pass

    def _verify_file_hash(self, file_path, expected_hash, hasher_cls):
        """校验本地文件哈希"""
        try:
            h = hasher_cls()
            with open(file_path, "rb") as f:
                while True:
                    data = f.read(8192)
                    if not data: 
                        break
                    h.update(data)
            return h.hexdigest().lower() == expected_hash.lower()
        except Exception:
            return False
