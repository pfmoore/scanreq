import os
import shutil
import tempfile
import sys
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path, PosixPath
from urllib.parse import urlparse
from urllib.request import urlopen
from zipfile import ZipFile

import build
import packaging.utils
from mousebender.simple import PYPI_INDEX, create_project_url, parse_archive_links
from packaging.requirements import Requirement
from shadwell.finder import Finder


class Candidate:
    def __init__(self, link):
        self.filename = link.filename
        self.url = link.url
        self.link = link

        self.is_wheel = self.filename.endswith(".whl")
        self.requires_python = link.requires_python
        self.is_yanked = link.yanked[0]

        if self.is_wheel:
            (
                self.name,
                self.version,
                _,
                self.tags,
            ) = packaging.utils.parse_wheel_filename(self.filename)
        else:
            self.name, self.version = packaging.utils.parse_sdist_filename(
                self.filename
            )
            self.tags = None

    def __repr__(self):
        return f"{self.name}[{self.version}] at {self.url}"


class TempDir(tempfile.TemporaryDirectory):
    def __init__(self):
        super().__init__()
        self.path = Path(self.name)
        self.idx = 0

    def __enter__(self):
        super().__enter__()
        return self

    def file(self):
        self.idx += 1
        return self.path / f"file-{self.idx}"

    def dir(self):
        self.idx += 1
        d = self.path / f"dir-{self.idx}"
        d.mkdir()
        return d


class Project:
    @property
    def metadata(self):
        raise NotImplemented("Subclasses must implement this")

    @property
    def name(self):
        return self.metadata["Name"]

    @property
    def version(self):
        return self.metadata["Version"]

    @property
    def requirements(self):
        return self.metadata.get_all("Requires-Dist", [])


def unpack_sdist(sdist: Path, target: Path):
    shutil.unpack_archive(sdist, target)
    # Technically, a sdist should contain a single subdirectory
    # with the source tree in it. But we cater here for no
    # subdirectory, or multiple levels (with no other content).
    while True:
        setup_py = target / "setup.py"
        pyproject_toml = target / "pyproject.toml"
        if setup_py.exists() or pyproject_toml.exists():
            return target
        content = list(target.iterdir())
        if len(content) != 1:
            break
        target = target / content[0]
        if not target.is_dir():
            break
    raise RuntimeError(f"Not a sdist: {sdist}")


class SourceProject(Project):
    def __init__(self, location):
        self.location = location
        self._metadata = None

    def get_src(self, tmp: TempDir):
        url = urlparse(self.location)
        if url.scheme in ("http", "https"):
            with urlopen(self.location) as f:
                src = tmp.path / url.path.rpartition("/")[-1]
                src.write_bytes(f.read())
        else:
            src = Path(self.location)
            if src.is_dir():
                return

        src = unpack_sdist(src, tmp.dir())
        return src

    def _get_metadata(self):
        if self._metadata:
            return
        with TempDir() as tmp:
            src = self.get_src(tmp)
            # Enormous hack to suppress build output
            build.pep517.default_subprocess_runner = build.pep517.quiet_subprocess_runner
            builder = build.ProjectBuilder(src)
            unsatisfied = builder.check_dependencies("wheel")
            if unsatisfied:
                print("The following build dependencies are missing:", unsatisfied)
                return None
            out_dir = tmp.dir()
            old_stdout = sys.stdout
            sys.stdout = BytesIO()
            wheel = builder.build("wheel", out_dir)
            sys.stdout = old_stdout
            self._metadata = WheelProject(wheel).metadata

    @property
    def metadata(self):
        self._get_metadata()
        return self._metadata


class WheelProject(Project):
    def __init__(self, location):
        self.location = location
        self._metadata = None

    def get_zip(self):
        name = self.location

        if os.path.isfile(name):
            return ZipFile(name)

        with urlopen(name) as fp:
            data = BytesIO(fp.read())
            return ZipFile(data)

    def _get_metadata(self):
        if self._metadata:
            return
        with self.get_zip() as z:
            for n in z.namelist():
                if n.endswith(".dist-info/METADATA"):
                    p = BytesParser()
                    self._metadata = p.parse(z.open(n), headersonly=True)
                    return
        raise RuntimeError("Wheel has no metadata")

    @property
    def metadata(self):
        self._get_metadata()
        return self._metadata


class X:
    def __init__(self):
        self.finder = Finder([self.get_candidates])

    def get_candidates(self, project):
        with urlopen(create_project_url(PYPI_INDEX, project)) as f:
            return [Candidate(l) for l in parse_archive_links(f.read().decode()) if l.filename.endswith((".tar.gz", ".whl"))]

    def versions(self, req):
        projects = {}
        for candidate in self.finder.get_candidates(req):
            if candidate.version in projects:
                continue
            if candidate.is_wheel:
                p = WheelProject(candidate.url)
            else:
                p = SourceProject(candidate.url)
            projects[candidate.version] = p
        return projects


if __name__ == "__main__":
    w = Requirement(sys.argv[1])
    x = X()
    results = x.versions(w)
    for ver, p in results.items():
        print(ver, p.version)
    raise SystemExit
    project = WheelProject(w)
    print(project)
    print(f"{project.name} {project.version}:\n{project.requirements}")
