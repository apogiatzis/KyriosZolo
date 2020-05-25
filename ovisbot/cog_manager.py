import os
import sys
import logging
import json
import shutil
import requests

from colorama import Fore
from git import Repo, Git
from pathlib import Path
from texttable import Texttable
from typing import Tuple, List, NoReturn

from ovisbot.db_models import CogDetails
from discord.ext.commands.errors import ExtensionNotLoaded

logger = logging.getLogger(__name__)


class CogAlreadyInstalledException(Exception):
    pass


class CogManager(object):
    """This class is responsible for loading arbitrary cogs (extensions)
    """

    def __init__(self, bot):
        self._bot = bot

    @property
    def cogs(self):
        return CogDetails.objects.all()

    def _builitin_cogs(self):
        """Returns a list of CogDetails objects for the builtin cogs"""
        builtin_cogs_dir = os.path.join(Path(__file__).resolve().parent, "extensions")
        builtin_cogs = self._create_cogs_from_path(builtin_cogs_dir)

        # Load builtin cogs from DB or create cog if does not exist
        cogs = []
        for cog in builtin_cogs:
            try:
                saved_cog = CogDetails.objects.get({"name": cog.name, "url": None})
                cogs.append(saved_cog)
            except CogDetails.DoesNotExist:
                cogs.append(cog)

        return cogs

    def _third_party_cogs(self):
        """Returns a list of CogDetails objects for the third party cogs
        as specified in the config"""
        return []

    def _create_cogs_from_path(self, path):
        """Creates CogDetails objects by traversing a given path"""
        return [
            CogDetails(name=diritem, local_path=os.path.join(path, diritem))
            for diritem in os.listdir(path)
            if os.path.isdir(os.path.join(path, diritem))
            and not diritem.startswith("__")
            and not diritem.endswith("__")
        ]

    def _load_cog_from_object(self, cog: CogDetails) -> CogDetails:
        """Loads a cog based ona CogDetails object"""
        try:
            sys.path.insert(1, cog.local_path)
            self._bot.load_extension(cog.name)
            logger.info(
                Fore.GREEN
                + "[Success] Extension: {0} from {1}".format(cog.name, cog.local_path)
            )
            cog.loaded = True
        except Exception as error:
            cog.enabled = False
            logger.info(
                Fore.RED
                + "[Failed] Extension: {0} from {1}".format(cog.name, cog.local_path)
            )
            logger.error("Cog `{0}` failed to load. Error: {1}".format(cog.name, error))
        return cog

    def _load_cog(self, path, name) -> bool:
        """Loads cog with given name in the given path
        """
        try:
            if os.path.join(path, name) not in sys.path:
                sys.path.insert(1, os.path.join(path, name))
            self._bot.load_extension(name)
            logger.info(
                Fore.GREEN + "[Success] Extension: {0} from {1}".format(name, path)
            )
            return (name, True)
        except Exception as error:
            logger.info(
                Fore.RED + "[Failed] Extension: {0} from {1}".format(name, path)
            )
            logger.error("Cog `{0}` failed to load. Error: {1}".format(name, error))
            return (name, False)

    def _load_cogs_in_path(self, path) -> Tuple[str, bool]:
        """
        Loads cogs in given path. In order for the cog to be loaded
        it must be defined in its own directory with the same
        filename.

        Returns:
             list: List of cogs along with a flag which indicates if they were loaded or not
        """
        return [
            self._load_cog(path, diritem)
            for diritem in os.listdir(path)
            if os.path.isdir(os.path.join(path, diritem))
            and not diritem.startswith("__")
            and not diritem.endswith("__")
        ]

    def cog_table(self):
        """Returns an ASCII table with details for installed cogs"""
        table = Texttable()
        table.set_deco(Texttable.HEADER)
        table.set_cols_dtype(["a", "a", "a", "a", "a"])  # automatic
        table.set_cols_align(["c", "c", "l", "l", "c"])
        table.add_rows(
            [
                ["enabled", "loaded", "name", "url", "open_source"],
                *[cog.tolist() for cog in self.cogs],
            ]
        )
        return table.draw()

    def is_cog_installed(self, name, url):
        """Returns true if the third party extension is already installed"""
        try:
            CogDetails.objects.get({"name": name, "url": url})
        except CogDetails.DoesNotExist:
            return False
        return True

    def parse_cog_spec(self, path):
        """
        Parses cog specification file from a path.
        Used to read metadata for third party cogs
        """
        data = None
        with open(os.path.join(path, "extension.json")) as json_file:
            data = json.load(json_file)
        return data

    def load_cogs(self) -> List[CogDetails]:
        """
        Loads builtin and installed cogs

        Returns:
            List[CogDetails]: A list of all the cogs attempted to load.
        """
        logger.info(Fore.YELLOW + "[+]" + Fore.RESET + " Loading Cogs...")

        builtin_cogs = self._builitin_cogs()
        third_party_cogs = self._third_party_cogs()

        for cog in builtin_cogs + third_party_cogs:
            if cog.enabled:
                cog = self._load_cog_from_object(cog)
                cog.save()

        return self.cogs

    def reset(self):
        """Deletes all third party installed cogs and resets builtin cogs"""
        for cog in self.cogs:
            if cog.enabled:
                try:
                    self._bot.unload_extension(cog.name)
                except ExtensionNotLoaded:
                    logger.error(
                        "Attempted to unload extension, without loading... Investigate this..."
                    )
            cog.delete()
        self.load_cogs()

    def disable_cog(self, name) -> NoReturn:
        """Disables the specified cog
        """
        cog = CogDetails.objects.get({"name": name})
        self._bot.unload_extension(cog.name)
        cog.enabled = False
        cog.save()

    def enable_cog(self, name) -> NoReturn:
        """Enables the specified cog
        """
        cog = CogDetails.objects.get({"name": name})
        self._load_cog_from_object(cog)
        cog.save()

    def install_cog_by_git_url(self, url, sshkey=None) -> CogDetails:
        """Installs a Cog from a git repository"""
        url = url.lower().strip()

        path = os.path.join(
            self._bot.config.THIRD_PARTY_COGS_INSTALL_DIR,
            url.split("/")[-1] + "_" + os.urandom(6).hex(),
        )

        if sshkey:
            logger.info(Fore.CYAN + "[+] Using SSH key to clone extension...")
            key_file_id = os.urandom(16).hex()
            git_ssh_identity_file = os.path.join("/tmp", "{0}.key".format(key_file_id))
            logger.info(Fore.CYAN + "[+] Privkey url: {0}".format(sshkey.private_key))

            r = requests.get(sshkey.private_key)
            with open(git_ssh_identity_file, "wb") as outfile:
                outfile.write(r.content)
            os.chmod(git_ssh_identity_file, 0o400)

            git_ssh_cmd = (
                'ssh -i %s -o "StrictHostKeyChecking no"' % git_ssh_identity_file
            )

            Repo.clone_from(
                url, path, branch="master", env={"GIT_SSH_COMMAND": git_ssh_cmd}
            )

            os.remove(git_ssh_identity_file)
        else:
            Repo.clone_from(url, path, branch="master")

        cog_spec = self.parse_cog_spec(path)
        name = cog_spec["name"]

        if self.is_cog_installed(name, url):
            shutil.rmtree(path, ignore_errors=True)
            raise CogAlreadyInstalledException(name)
        else:
            cog = CogDetails(
                name=name,
                local_path=path,
                url=url,
                open_source=False if sshkey else True,
            )
            self._load_cog_from_object(cog)
            cog.save()
