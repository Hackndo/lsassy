from lsassy.logger import lsassy_logger


class IExec:
    """
    Interface for remote execution
    """
    debug_privilege = False
    kerberos_support = True

    def __init__(self, session):
        self.session = session
        

    def exec(self, command):
        """
        To be implemented in all exec modules
        :param command: Command to be executed on remote host
        """
        lsassy_logger.info("Executing using {}".format(self.__module__))
        if not self.kerberos_support and self.session.kerberos is True:
            lsassy_logger.error("Module {} does not support Kerberos authentication".format(self.__module__))
            return False
        return True

    def clean(self):
        """
        To be implemented in all exec modules
        """
        pass
