#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gitlab-export.py - Gitlab-to-Gitlab migration with recursive export/import.

Python program to recursively export and import Gitlab groups, subgroups and their projects.
Basically, to automatize Gitlab-to-Gitlab migration.

Usage:
  gitlab-export.py [options] export --server-url=URL --private-token=TOKEN --root=path <directory>
  gitlab-export.py [options] import --server-url=URL --private-token=TOKEN [--root=path] <directory>
  gitlab-export.py -h | --help
  gitlab-export.py --version

Arguments:
  directory         Folder for export files.

Options:
  --delay=SEC       Refresh delay for Gitlab status querying [default: 15].
  -h --help         Show this screen.
  --logfile=FILE    Logging to FILE, otherwise use STDOUT.
  --no-color        No colored log output.
  --no-groups       Do not import groups, only projects.
  --no-ssl-verify   Do not verify HTTPS/TLS certificates.
  --server-url=URL  Gitlab URL, e.g., https://gitlab.example.com
  --root=PATH       Gitlab path.
  --token=TOKEN     Gitlab API private access token (type 'api').
  --version         Show version.
"""
##
# LICENSE:
##
# Copyright (c) 2022 by Ixtalo, ixtalo@gmail.com
##
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
##
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
##
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
##
import json
import logging
import os
import sys
from fnmatch import fnmatch
from glob import iglob
from pathlib import Path
from time import sleep
from typing import Union

import colorlog
import urllib3
from docopt import docopt
# https://pypi.org/project/python-gitlab/
# https://python-gitlab.readthedocs.io/en/stable/
from gitlab import Gitlab
from gitlab.exceptions import GitlabError, GitlabGetError
from gitlab.v4.objects import Group, Project

__appname__ = "gitlab_export_import"
__version__ = "1.0.0"
__date__ = "2022-10-21"
__updated__ = "2022-10-21"
__author__ = "Ixtalo"
__email__ = "ixtalo@gmail.com"
__license__ = "AGPL-3.0+"

LOGGING_STREAM = sys.stdout
DEBUG = bool(os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"))

# check for Python3
if sys.version_info < (3, 8):
    sys.stderr.write("Minimum required version is Python 3.8!\n")
    sys.exit(1)


class GitlabImportExport:
    """Gitlab Export-Import."""

    METADATA_FILENAME = "metadata.json"
    FILENAME_PATTERN_GROUP = "group_*.tar.gz"
    FILENAME_PATTERN_PROJECT = "project_*.tar.gz"

    def __init__(self, gl: Gitlab, export_folder: Path, delay_seconds: float = 15):
        """Import and export of Gitlab groups and projects."""
        self.gl = gl
        assert delay_seconds >= 1, "Delay between download checks must be a meaningful duration!"
        self.delay_seconds = delay_seconds
        self.export_folder = export_folder

    def exporting(self, gitlab_path: str):
        """Recursive export of Gitlab groups and projects."""
        # 1. export root group
        group = self._export_group(gitlab_path)
        # 2. export projects inside root group
        self._export_projects(group)
        # 3. recursively export subprojects (and their projects)
        self._export_subprojects_recursive(group)

    # -------------------------------------------------------------------------
    # Export
    # -------------------------------------------------------------------------

    def export_project(self, project: Project, output_path: Path = None):
        """Export a project and download it to the filesystem."""
        assert isinstance(project, Project)
        if not output_path:
            # the initial main-group export takes the initial class-level output path
            output_path = self.export_folder

        logging.info("Project export for '%s' (%d, '%s')...",
                     project.path_with_namespace, project.id, project.name)

        # initiate the export process
        try:
            exporter = project.exports.create()
            # a first refresh() is needed to fill the exporter object with all attributes
            exporter.refresh()
        except GitlabError as ex:
            logging.error("Problem creating export for '%s': %s", project.path, ex.error_message)
            return
        logging.debug("exporter: %s", exporter)

        # wait till the download is ready
        try:
            while exporter and exporter.export_status != 'finished':
                logging.debug("waiting %.1f seconds till next status update...", self.delay_seconds)
                sleep(self.delay_seconds)
                exporter.refresh()
            logging.debug("exporter: %s", exporter)
        except GitlabError as ex:
            logging.error("Problem getting status for '%s': %s", project.path, ex.error_message)
            return

        # project.path is just the plain single path-component, not the full_path
        output_filepath_project = output_path.joinpath(f"project_{project.path}.tar.gz")
        logging.info("Downloading export for '%s': %s",
                     project.path, output_filepath_project.resolve())
        try:
            with output_filepath_project.open("wb") as fout:
                exporter.download(streamed=True, action=fout.write)
                logging.info("Download finished for '%s'", project.path)
        except Exception as ex:
            logging.exception("Problem while downloading: %s", ex, exc_info=ex)

        # create project metadata file
        self._write_metadata_file(
            project, self.__get_filepath_project_metadata(output_filepath_project))

    def _export_group(self, gitlab_path: str) -> Group:
        group = self.get_group(gitlab_path)
        logging.debug("group to export: %s", group)
        if not group:
            raise RuntimeError(f"No such group: {gitlab_path}")

        logging.info("Creating group export for '%s' (path '%s') ...", group.name, group.path)
        exporter = None
        try:
            exporter = group.exports.create()
        except GitlabError as ex:
            logging.exception("Problem creating export: %s", ex.error_message, exc_info=ex)

        logging.debug("exporter: %s", exporter)

        # give it some time to do the export
        # (exporting groups is fast because only metadata, no projects etc.)
        logging.debug("waiting %.1f seconds (delay time)...", self.delay_seconds)
        sleep(self.delay_seconds)

        # modify output path: exportfolder/date_time/maingroup/.../
        self.export_folder = self.export_folder.joinpath(group.path)
        logging.debug("creating output path '%s' ...", self.export_folder.resolve())
        os.makedirs(self.export_folder, exist_ok=True)

        # Gitlab sends the export as tar.gz archive
        output_file = self.export_folder.joinpath(f"group_{group.path}.tar.gz")
        logging.info("Downloading group export to '%s' ...", output_file.resolve())
        with output_file.open("wb") as fout:
            exporter.download(streamed=True, action=fout.write)

        # write group metadata file
        self._write_metadata_file(group, self.__get_filepath_group_metadata(self.export_folder))

        logging.info("Group export finished for '%s' (%s)", group.path, exporter.message)
        return group

    def _export_projects(self, group: Group, output_path: Path = None):
        assert isinstance(group, Group)
        if not output_path:
            # the initial main-group export takes the initial class-level output path
            output_path = self.export_folder

        projects = group.projects.list(all=True)
        logging.info("#%d projects in '%s'", len(projects), group.full_path)
        for project in projects:
            # the project-object is a lazy proxy object - get the real one
            project_full_obj = self.gl.projects.get(project.id)
            self.export_project(project_full_obj, output_path)

    def _export_subprojects_recursive(self, group: Group, output_path: Path = None):
        assert isinstance(group, Group)
        if not output_path:
            # the initial main-group export takes the initial class-level output path
            output_path = self.export_folder

        subgroups = group.subgroups.list()
        for subgroup in subgroups:
            # the subgroup-object is a lazy proxy object - get the real one
            subgroup_full_obj = self.gl.groups.get(subgroup.id)

            # create subdir for subgroup
            output_path_subdir = output_path.joinpath(subgroup_full_obj.path)
            os.makedirs(output_path_subdir, exist_ok=True)

            # create group metadata file
            self._write_metadata_file(subgroup_full_obj,
                                      self.__get_filepath_group_metadata(output_path_subdir))

            # shallow call (only the projects in current group)
            self._export_projects(subgroup_full_obj, output_path_subdir)

            # recursive call (subgroups and sub-projects)
            self._export_subprojects_recursive(subgroup_full_obj, output_path_subdir)

    # -------------------------------------------------------------------------
    # Export
    # -------------------------------------------------------------------------

    def importing(self, gitlab_path: str, no_groups: bool = False):
        """Import Gitlab group and project exports (by this very script) into Gitlab."""
        if not no_groups:
            result = self._import_groups(gitlab_path)
            if not result:
                return
        self._import_projects(gitlab_path)

    def import_project(self, filepath: Path, name: str, slug, namespace: str):
        """Import project archive into Gitlab."""
        # do the import by sending the file content as binary data stream
        import_id = self.__import_project_upload(filepath, name, slug, namespace)
        if import_id:
            # ask the ProjectImportManager for the status and wait till it's done
            self.__import_project_wait_done(import_id)

    def __import_project_upload(self, filepath: Path, name: str, slug, namespace: str):
        """Do the import by sending the file content as binary data stream."""
        with filepath.open("rb") as fin:
            try:
                result = self.gl.projects.import_project(
                    file=fin, name=name, path=slug, namespace=namespace)
                logging.debug("import result: %s", result)
                import_id = result["id"]
                logging.info("Upload done. Remote import job is now in progress (id:%d)", import_id)
                return import_id
            except GitlabError as ex:
                logging.exception("Problem importing project '%s': %s",
                                  f"{namespace}/{slug}", ex.error_message, exc_info=ex)
        return None

    def __import_project_wait_done(self, import_id):
        """Ask the ProjectImportManager for the status and wait till it's done."""
        try:
            importer = self.gl.projects.get(import_id, lazy=True).imports.get()
            while importer.import_status != "finished":
                logging.debug("waiting %.1f seconds till next status update...", self.delay_seconds)
                sleep(self.delay_seconds)
                importer.refresh()
            logging.debug("project_import_job: %s", importer)
            logging.info("Import finished of '%s'", importer.path_with_namespace)
        except GitlabError as ex:
            logging.exception("Problem importing project: %s", ex.error_message, exc_info=ex)

    def _import_project(self, filepath: Path, namespace: str):
        """Import project from archive file, reading parameters from metadata file."""
        metadata = self._read_metadata_file(self.__get_filepath_project_metadata(filepath))
        slug = metadata["path"]
        name = metadata["name"]
        path_with_namespace = f"{namespace}/{slug}"
        logging.debug("project.path_with_namespace: %s", path_with_namespace)

        if self.get_project(path_with_namespace):
            logging.warning("Skipping already existing project: %s", path_with_namespace)
            return

        logging.info("Importing project '%s' (%d MB) to '%s' ...",
                     name, filepath.stat().st_size / 1024.0 / 1024.0, path_with_namespace)
        self.import_project(filepath, name, slug, namespace)

    def _import_projects(self, gitlab_root: str = ""):
        """Import Gitlab projects from a folder structure."""
        # path-component name of this group (not the whole namespace), e.g., just "mysubgroup1"
        main_group_path = self._read_metadata_file(
            self.__get_filepath_group_metadata(self.export_folder))["path"]
        logging.debug("main_group_path: %s", main_group_path)

        logging.debug("Scanning for project export archives in '%s' ...",
                      self.export_folder.resolve())
        for root, _, files in os.walk(self.export_folder):
            logging.debug("Handling folder '%s' ...", Path(root).resolve())

            # make root relative to the export_folder (strip export_folder from the front)
            root_relative = Path(root).relative_to(self.export_folder)
            # rootgroupname/subgroup1/subgroup2  (main_group + subgroups)
            namespace = str(Path(main_group_path).joinpath(root_relative))
            # make sure it's all '/' in paths/namespaces, not '\', needed for MS Windows
            namespace = namespace.replace("\\", "/")

            # prepend user-specified Gitlab root (if given on CLI)
            if gitlab_root:
                namespace = f"{gitlab_root}/{namespace}"

            logging.debug("namespace: %s", namespace)

            for filename in files:
                if fnmatch(filename, "*.json") or fnmatch(filename, self.FILENAME_PATTERN_GROUP):
                    continue
                if not fnmatch(filename, self.FILENAME_PATTERN_PROJECT):
                    logging.warning("Skipping non-project archive: %s", filename)
                    continue

                filepath = Path(root).joinpath(filename)
                self._import_project(filepath, namespace)

    def _import_groups(self, gitlab_root: str = ""):
        """Import groups from export file."""
        self.__check_is_export_folder(self.export_folder)

        metadata = self._read_metadata_file(self.__get_filepath_group_metadata(self.export_folder))
        main_group_name = metadata["name"]
        main_group_path = metadata["path"]

        # root has gitlab_group_parent_id=None
        gitlab_group_parent_id = None

        # try to find the Gitlab parent group
        if gitlab_root:
            group = self.get_group(gitlab_root)
            if group:
                gitlab_group_parent_id = group.id
                logging.info("Parent group found: %s (%d, '%s')",
                             gitlab_root, gitlab_group_parent_id, group.name)
            else:
                logging.error("No such group '%s' (for this access token)! (make sure it exists!)",
                              gitlab_root)
                return False

        if gitlab_group_parent_id:
            logging.info("Importing group '%s' with path '%s' as subgroup of parent_id=%s ...",
                         main_group_name, main_group_path, str(gitlab_group_parent_id))
        else:
            logging.info("Importing group '%s' with path '%s' ...",
                         main_group_name, main_group_path)

        # get first "group_*.tar.gz" file
        export_file = self.__get_group_exportfile(self.export_folder)
        logging.debug("group export_file: %s", export_file)

        export_filepath = Path(export_file)
        logging.info("Loading from file '%s' ...", export_filepath)
        with export_filepath.open("rb") as fin:
            self.gl.groups.import_group(fin, path=main_group_path, name=main_group_name,
                                        parent_id=str(gitlab_group_parent_id))

        logging.info("Group import of '%s' done.", main_group_name)

        return True

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def get_project(self, path_with_namespace: str) -> Union[Project, None]:
        """Get the Gitlab data object for a project."""
        try:
            return self.gl.projects.get(path_with_namespace, with_custom_attributes=False)
        except GitlabGetError:
            return None

    def get_group(self, gitlab_path: str) -> Union[Group, None]:
        """Get the Gitlab data object for a group."""
        try:
            return self.gl.groups.get(gitlab_path, with_custom_attributes=False,
                                      with_projects=False)
        except GitlabGetError:
            return None

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def __get_filepath_group_metadata(self, path: Path) -> Path:
        return path.joinpath(self.METADATA_FILENAME)

    @staticmethod
    def __get_filepath_project_metadata(path: Path) -> Path:
        return path.with_name(f"{path.name}.json")

    def __check_is_export_folder(self, path: Path):
        if not self.__get_filepath_group_metadata(path).exists():
            raise RuntimeError(f"Path has no metadata file '{self.METADATA_FILENAME}'!")

    @staticmethod
    def __get_group_exportfile(path: Path):
        return next(iglob(str(path.joinpath("group_*.tar.gz").resolve())))

    @staticmethod
    def _write_metadata_file(obj: Union[Project, Group], filepath: Path):
        metadata_keys = ("id", "parent_id", "created_at", "name", "full_name", "path", "full_path")
        metadata = {}
        for key in metadata_keys:
            try:
                metadata[key] = getattr(obj, key)
            except AttributeError:
                pass
        assert "name" in metadata
        assert "path" in metadata
        logging.debug("writing metadata file '%s' ...", filepath.resolve())
        with filepath.open("w", encoding="utf8") as fout:
            json.dump(metadata, fout, indent=4)

    @staticmethod
    def _read_metadata_file(filepath: Path) -> dict:
        logging.debug("loading metadata file '%s' ...", filepath)
        with filepath.open("r", encoding="utf8") as fin:
            return json.load(fin)


def __setup_logging(log_file: str = None, verbose=False, no_color=False):
    if log_file:
        # pylint: disable=consider-using-with
        stream = open(log_file, "a", encoding="utf8")
        no_color = True
    else:
        stream = LOGGING_STREAM
    handler = colorlog.StreamHandler(stream=stream)

    format_string = "%(log_color)s%(asctime)s %(levelname)-8s %(message)s"
    formatter = colorlog.ColoredFormatter(
        format_string, datefmt="%Y-%m-%d %H:%M:%S", no_color=no_color)
    handler.setFormatter(formatter)

    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    if verbose or log_file:
        logging.getLogger("").setLevel(logging.INFO)
    if DEBUG:
        logging.getLogger("").setLevel(logging.DEBUG)


def main():
    """Run main program entry.

    :return: exit/return code
    """
    version_string = f"Gitlab-Export-Import {__version__} ({__updated__})"
    arguments = docopt(__doc__, version=version_string)
    # print(arguments)
    arg_directory = arguments["<directory>"]
    arg_logfile = arguments["--logfile"]
    arg_nocolor = arguments["--no-color"]
    arg_host = arguments["--server-url"]
    arg_token = arguments["--private-token"]
    arg_root = arguments["--root"]
    arg_delay = float(arguments["--delay"])
    arg_nogroups = arguments["--no-groups"]
    arg_no_ssl_verify = arguments["--no-ssl-verify"]

    __setup_logging(arg_logfile, verbose=True, no_color=arg_nocolor)
    logging.info(version_string)

    export_folder = Path(arg_directory)
    logging.info("export folder: %s", export_folder.resolve())
    if not export_folder.exists():
        raise FileNotFoundError(export_folder)
    if not export_folder.is_dir():
        raise NotADirectoryError(export_folder)

    if arg_no_ssl_verify:
        # disable TLS warnings
        # https://urllib3.readthedocs.io/en/1.26.x/advanced-usage.html#ssl-warnings
        logging.warning("Disabled TLS self-signed certificate warnings.")
        urllib3.disable_warnings()

    # Gitlab API connection
    with Gitlab(arg_host, private_token=arg_token, ssl_verify=not arg_no_ssl_verify,
                keep_base_url=True) as gl:

        logging.info("Gitlab version: %s", gl.version())
        gl.auth()   # make sure we can authenticate

        gei = GitlabImportExport(gl, export_folder, delay_seconds=arg_delay)

        if arguments["export"]:
            gei.exporting(arg_root)
        elif arguments["import"]:
            gei.importing(arg_root, no_groups=arg_nogroups)
        else:
            raise RuntimeError("Invalid mode!")

    logging.info("Done.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
