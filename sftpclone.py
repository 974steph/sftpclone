"""SFTPClone: sync local and remote directories."""

# Python 2.7 backward compatibility
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import

import paramiko
import os
from os.path import join
import sys
import errno
from stat import S_ISDIR, S_ISLNK, S_ISREG, S_IMODE, S_IFMT
import argparse
import logging
from getpass import getuser, getpass
import glob
import socket

logger = None


def configure_logging(level=logging.DEBUG):
    """Configure the module logging engine."""
    logger = logging.getLogger(__name__)
    logger.setLevel(level)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger


class SFTPClone(object):

    """The SFTPClone class."""

    def __init__(self, local_path, remote_url,
                 key=None, port=None, fix_symlinks=False,
                 ssh_config_path=None, exclude_file=None):
        """Init the needed parameters and the SFTPClient."""
        self.local_path = os.path.realpath(os.path.expanduser(local_path))
        self.logger = logger or configure_logging()

        if not os.path.exists(self.local_path):
            self.logger.error("Local path MUST exist. Exiting.")
            sys.exit(1)

        if exclude_file:
            with open(exclude_file) as f:
                # As in rsync's exclude from, ignore lines with leading ; and #
                # and treat each path as relative (thus by removing the leading
                # /)
                exclude_list = [
                    line.rstrip().lstrip("/")
                    for line in f
                    if not line.startswith((";", "#"))
                ]

                # actually, is a set of excluded files
                self.exclude_list = {
                    g
                    for pattern in exclude_list
                    for g in glob.glob(join(self.local_path, pattern))
                }
        else:
            self.exclude_list = set()

        if '@' in remote_url:
            self.username, self.hostname = remote_url.split('@', 1)
        else:
            self.username, self.hostname = None, remote_url

        self.hostname, self.remote_path = self.hostname.split(':', 1)

        self.password = None
        if self.username and ':' in self.username:
            self.username, self.password = self.username.split(':', 1)

        self.port = None

        if ssh_config_path:
            try:
                with open(os.path.expanduser(ssh_config_path)) as c_file:
                    ssh_config = paramiko.SSHConfig()
                    ssh_config.parse(c_file)
                    c = ssh_config.lookup(self.hostname)

                    self.hostname = c.get("hostname", self.hostname)
                    self.username = c.get("user", self.username)
                    self.port = int(c.get("port", port))
                    key = c.get("identityfile", key)
            except Exception as e:
                # it could be safe to continue anyway,
                # because parameters could have been manually specified
                self.logger.error(
                    "Error while parsing ssh_config file: %s. Trying to continue anyway...", e
                )

        # Set default values
        if not self.username:
            self.username = getuser()  # defaults to current user

        if not self.port:
            self.port = port if port else 22

        self.chown = False
        self.fix_symlinks = fix_symlinks if fix_symlinks else False

        self.pkey = None
        if key and not self.password:
            key = os.path.expanduser(key)
            try:
                self.pkey = paramiko.RSAKey.from_private_key_file(key)
            except paramiko.PasswordRequiredException:
                pk_password = getpass(
                    "It seems that your private key is encrypted. Please enter your password: "
                )
                try:
                    self.pkey = paramiko.RSAKey.from_private_key_file(
                        key, pk_password)
                except paramiko.ssh_exception.SSHException:
                    self.logger.error(
                        "Incorrect passphrase. Cannot decode private key."
                    )
                    sys.exit(1)
        elif not key and not self.password:
            self.logger.error(
                "You need to specify a password or an identity."
            )
            sys.exit(1)

        # only root can change file owner
        if self.username == 'root':
            self.chown = True

        try:
            self.transport = paramiko.Transport((self.hostname, self.port))
        except socket.gaierror:
            self.logger.error(
                "Hostname not known. Are you sure you inserted it correctly?")
            sys.exit(1)

        self.transport.connect(
            username=self.username,
            password=self.password,
            pkey=self.pkey)
        self.sftp = paramiko.SFTPClient.from_transport(self.transport)

        if (self.remote_path.startswith("~")):
            # nasty hack to let getcwd work without changing dir!
            self.sftp.chdir('.')
            self.remote_path = self.remote_path.replace(
                "~", self.sftp.getcwd())  # home is the initial sftp dir

    def _file_need_upload(self, l_st, r_st):
        return True if \
            (l_st.st_size != r_st.st_size) or (int(l_st.st_mtime) != r_st.st_mtime) \
            else False

    def _match_modes(self, remote_path, l_st):
        """Match mod, utime and uid/gid with locals one."""
        self.sftp.chmod(remote_path, S_IMODE(l_st.st_mode))
        self.sftp.utime(remote_path, (l_st.st_atime, l_st.st_mtime))

        if self.chown:
            self.sftp.chown(remote_path, l_st.st_uid, l_st.st_gid)

    def file_upload(self, local_path, remote_path, l_st):
        """Upload local_path to remote_path and set permission and mtime."""
        self.sftp.put(local_path, remote_path)
        self._match_modes(remote_path, l_st)

    def _must_be_deleted(self, local_path, r_st):
        """Return True if the remote correspondent of local_path has to be deleted.

        i.e. if it doesn't exists locally or if it has a different type from the remote one."""
        # if the file doesn't exists
        if not os.path.lexists(local_path):
            return True

        # or if the file type is different
        l_st = os.lstat(local_path)
        if S_IFMT(r_st.st_mode) != S_IFMT(l_st.st_mode):
            return True

        return False

    def remote_delete(self, remote_path, r_st):
        """Remove the remote directory node."""
        # If it's a directory, then delete content and directory
        if S_ISDIR(r_st.st_mode):
            for item in self.sftp.listdir_attr(remote_path):
                full_path = join(remote_path, item.filename)
                self.remote_delete(full_path, item)
            self.sftp.rmdir(remote_path)

        # Or simply delete files
        else:
            try:
                self.sftp.remove(remote_path)
            except FileNotFoundError as e:
                self.logger.error(
                    "error while removing {}. trace: {}".format(remote_path, e)
                )

    def check_for_deletion(self, relative_path=None):
        """Traverse the entire remote_path tree.

        Find files/directories that need to be deleted,
        not being present in the local folder.
        """
        if not relative_path:
            relative_path = str()  # root of shared directory tree

        remote_path = join(self.remote_path, relative_path)
        local_path = join(self.local_path, relative_path)

        for remote_st in self.sftp.listdir_attr(remote_path):
            r_lstat = self.sftp.lstat(join(remote_path, remote_st.filename))

            inner_remote_path = join(remote_path, remote_st.filename)
            inner_local_path = join(local_path, remote_st.filename)

            # check if remote_st is a symlink
            # otherwise could delete file outside shared directory
            if S_ISLNK(r_lstat.st_mode):
                if (self._must_be_deleted(inner_local_path, r_lstat)):
                    self.remote_delete(inner_remote_path, r_lstat)
                continue

            if self._must_be_deleted(inner_local_path, remote_st):
                self.remote_delete(inner_remote_path, remote_st)
            elif S_ISDIR(remote_st.st_mode):
                self.check_for_deletion(
                    join(relative_path, remote_st.filename)
                )

    def create_update_symlink(self, link_destination, remote_path):
        """Create a new link pointing to link_destination in remote_path position."""
        try:
            try:  # check if the remote link exists
                remote_link = self.sftp.readlink(remote_path)

                # if it does exist and it is different, update it
                if link_destination != remote_link:
                    self.sftp.remove(remote_path)
                    self.sftp.symlink(link_destination, remote_path)
            except IOError:  # if not, create it and done!
                self.sftp.symlink(link_destination, remote_path)
        # sometimes symlinking fails if absolute path are "too" different
        except OSError as e:
        # Sadly, nothing we can do about it.
            self.logger.error("error while symlinking {} to {}: {}".format(
                remote_path, link_destination, e))

    def node_check_for_upload_create(self, relative_path, f):
        """Check if the given directory tree node has to be uploaded/created on the remote folder."""
        if not relative_path:
            # we're at the root of the shared directory tree
            relative_path = str()

        # the (absolute) local address of f.
        local_path = join(self.local_path, relative_path, f)
        l_st = os.lstat(local_path)

        if (local_path) in self.exclude_list:
            self.logger.info("Skipping excluded file %s.", local_path)
            return

        # the (absolute) remote address of f.
        remote_path = join(self.remote_path, relative_path, f)

        # First case: f is a directory
        if S_ISDIR(l_st.st_mode):
            # we check if the folder exists on the remote side
            # it has to be a folder, otherwise it would have already been
            # deleted
            try:
                r_st = self.sftp.stat(remote_path)
            except IOError:  # it doesn't exist yet on remote side
                self.sftp.mkdir(remote_path)

            self._match_modes(remote_path, l_st)

            # now, we should traverse f too (recursion magic!)
            self.check_for_upload_create(join(relative_path, f))

        # Second case: f is a symbolic link
        elif S_ISLNK(l_st.st_mode):
            # read the local link
            local_link = os.readlink(local_path)
            absolute_local_link = os.path.realpath(local_link)

            # is it absolute?
            is_absolute = local_link.startswith("/")
            # and does it point inside the shared directory?
            # add trailing slash (security)
            trailing_local_path = join(self.local_path, '')
            relpath = os.path.commonprefix(
                [absolute_local_link,
                 trailing_local_path]
            ) == trailing_local_path

            if relpath:
                relative_link = absolute_local_link[len(trailing_local_path):]
            else:
                relative_link = None

            """
            # Refactor them all, be efficient!

            # Case A: absolute link pointing outside shared directory
            #   (we can only update the remote part)
            if is_absolute and not relpath:
                self.create_update_symlink(local_link, remote_path)

            # Case B: absolute link pointing inside shared directory
            #   (we can leave it as it is or fix the prefix to match the one of the remote server)
            elif is_absolute and relpath:
                if self.fix_symlinks:
                    self.create_update_symlink(
                        join(
                            self.remote_path,
                            relative_link,
                        ),
                        remote_path
                    )
                else:
                    self.create_update_symlink(local_link, remote_path)

            # Case C: relative link pointing outside shared directory
            #   (all we can do is try to make the link anyway)
            elif not is_absolute and not relpath:
                self.create_update_symlink(local_link, remote_path)

            # Case D: relative link pointing inside shared directory
            #   (we preserve the relativity and link it!)
            elif not is_absolute and relpath:
                self.create_update_symlink(local_link, remote_path)
            """

            if is_absolute and relpath:
                if self.fix_symlinks:
                    self.create_update_symlink(
                        join(
                            self.remote_path,
                            relative_link,
                        ),
                        remote_path
                    )
            else:
                self.create_update_symlink(local_link, remote_path)

        # Third case: regular file
        elif S_ISREG(l_st.st_mode):
            try:
                r_st = self.sftp.lstat(remote_path)
                if self._file_need_upload(l_st, r_st):
                    self.file_upload(local_path, remote_path, l_st)
            except IOError as e:
                if e.errno == errno.ENOENT:
                    self.file_upload(local_path, remote_path, l_st)

        # Anything else.
        else:
            self.logger.warning("Skipping unsupported file %s.", local_path)

    def check_for_upload_create(self, relative_path=None):
        """Traverse the relative_path tree and check for files that need to be uploaded/created.

        Relativity here refers to the shared directory tree."""
        for f in os.listdir(
            join(
                self.local_path, relative_path) if relative_path else self.local_path
        ):
            self.node_check_for_upload_create(relative_path, f)

    def run(self):
        """Run the sync.

        Confront the local and the remote directories and perform the needed changes."""
        # first check for items to be removed
        self.check_for_deletion()

        # now scan local for items to upload/create
        self.check_for_upload_create()


def create_parser():
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description='Sync a local and a remote folder through SFTP.'
    )

    parser.add_argument(
        "path",
        type=str,
        metavar="local-path",
        help="the path of the local folder",
    )

    parser.add_argument(
        "remote",
        type=str,
        metavar="user[:password]@hostname:remote-path",
        help="the ssh-url ([user[:password]@]hostname:remote-path) of the remote folder. "
             "The hostname can be specified as a ssh_config's hostname too. "
             "Every missing information will be gathered from there",
    )

    parser.add_argument(
        "-k",
        "--key",
        metavar="private-key-path",
        default="~/.ssh/id_rsa",
        type=str,
        help="private key identity path (defaults to ~/.ssh/id_rsa)"
    )

    parser.add_argument(
        "-l",
        "--logging",
        choices=['CRITICAL',
                 'ERROR',
                 'WARNING',
                 'INFO',
                 'DEBUG',
                 'NOTSET'],
        default='ERROR',
        help="set logging level"
    )

    parser.add_argument(
        "-p",
        "--port",
        default=22,
        type=int,
        help="SSH remote port (defaults to 22)"
    )

    parser.add_argument(
        "-f",
        "--fix-symlinks",
        action="store_true",
        help="fix symbolic links on remote side"
    )

    parser.add_argument(
        "-c",
        "--ssh-config",
        metavar="ssh-config-path",
        default="~/.ssh/config",
        type=str,
        help="path of the ssh-configuration file"
    )

    parser.add_argument(
        "-e",
        "--exclude-from",
        metavar="exclude-from-file-path",
        type=str,
        help="exclude files matching pattern in exclude-from-file-path"
    )
    return parser


def main(args=None):
    """The main."""
    parser = create_parser()

    args = vars(parser.parse_args(args))

    log_mapping = {
        'CRITICAL': logging.CRITICAL,
        'ERROR': logging.ERROR,
        'WARNING': logging.WARNING,
        'INFO': logging.INFO,
        'DEBUG': logging.DEBUG,
        'NOTSET': logging.NOTSET,
    }
    log_level = log_mapping[args['logging']]

    global logger
    logger = configure_logging(log_level)

    args_mapping = {
        "path": "local_path",
        "remote": "remote_url",
        "port": "port",
        "key": "key",
        "fix_symlinks": "fix_symlinks",
        "ssh_config": "ssh_config_path",
        "exclude_from": "exclude_file"
    }

    kwargs = {args_mapping[k]: v for k, v in args.items() if v and k != 'logging'}

    sync = SFTPClone(
        **kwargs
    )
    sync.run()


if __name__ == '__main__':
    main()
