import datetime
import enum
import hashlib
import logging
import pathlib
import typing

from collections import defaultdict, namedtuple
from typing import TYPE_CHECKING, AbstractSet, ClassVar, Dict, List, Optional, Union


if TYPE_CHECKING:
    from hashlib import _Hash

from pydantic import Field, validator
from typing_extensions import Literal

from conda_lock.common import ordered_union, relative_path
from conda_lock.models import StrictModel
from conda_lock.models.channel import Channel


logger = logging.getLogger(__name__)


class DependencySource(StrictModel):
    type: Literal["url"]
    url: str


LockKey = namedtuple("LockKey", ["manager", "name", "platform"])


class HashModel(StrictModel):
    md5: Optional[str] = None
    sha256: Optional[str] = None


class LockedDependency(StrictModel):
    name: str
    version: str
    manager: Literal["conda", "pip"]
    platform: str
    dependencies: Dict[str, str] = {}
    url: str
    hash: HashModel
    optional: bool = False
    category: str = "main"
    source: Optional[DependencySource] = None
    build: Optional[str] = None

    def key(self) -> LockKey:
        return LockKey(self.manager, self.name, self.platform)

    @validator("hash")
    def validate_hash(cls, v: HashModel, values: Dict[str, typing.Any]) -> HashModel:
        if (values["manager"] == "conda") and (v.md5 is None):
            raise ValueError("conda package hashes must use MD5")
        return v


class MetadataOption(enum.Enum):
    TimeStamp = "timestamp"
    GitSha = "git_sha"
    GitUserName = "git_user_name"
    GitUserEmail = "git_user_email"
    InputMd5 = "input_md5"
    InputSha = "input_sha"


class TimeMeta(StrictModel):
    """Stores information about when the lockfile was generated."""

    created_at: str = Field(..., description="Time stamp of lock-file creation time")

    @classmethod
    def create(cls) -> "TimeMeta":
        return cls(
            created_at=datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        )


class GitMeta(StrictModel):
    """
    Stores information about the git repo the lockfile is being generated in (if applicable) and
    the git user generating the file.
    """

    git_user_name: Optional[str] = Field(
        default=None, description="Git user.name field of global config"
    )
    git_user_email: Optional[str] = Field(
        default=None, description="Git user.email field of global config"
    )
    git_sha: Optional[str] = Field(
        default=None,
        description=(
            "sha256 hash of the most recent git commit that modified one of the input files for "
            + "this lockfile"
        ),
    )

    @classmethod
    def create(
        cls,
        metadata_choices: AbstractSet[MetadataOption],
        src_files: List[pathlib.Path],
    ) -> "GitMeta | None":
        try:
            import git
        except ImportError:
            return None

        git_sha: "str | None" = None
        git_user_name: "str | None" = None
        git_user_email: "str | None" = None

        try:
            repo = git.Repo(search_parent_directories=True)  # type: ignore
            if MetadataOption.GitSha in metadata_choices:
                most_recent_datetime: Optional[datetime.datetime] = None
                for src_file in src_files:
                    relative_src_file_path = relative_path(
                        pathlib.Path(repo.working_tree_dir), src_file  # type: ignore
                    )
                    commit = list(
                        repo.iter_commits(paths=relative_src_file_path, max_count=1)
                    )[0]
                    if repo.is_dirty(path=relative_src_file_path):
                        logger.warning(
                            "One of the inputs to conda-lock is dirty, using commit hash of head +"
                            ' "dirty"'
                        )
                        git_sha = f"{repo.head.object.hexsha}-dirty"
                        break
                    else:
                        if (
                            most_recent_datetime is None
                            or most_recent_datetime < commit.committed_datetime
                        ):
                            most_recent_datetime = commit.committed_datetime
                            git_sha = commit.hexsha
            if MetadataOption.GitUserName in metadata_choices:
                git_user_name = repo.config_reader().get_value("user", "name", None)  # type: ignore
            if MetadataOption.GitUserEmail in metadata_choices:
                git_user_email = repo.config_reader().get_value("user", "email", None)  # type: ignore
        except git.exc.InvalidGitRepositoryError:  # type: ignore
            pass

        if any([git_sha, git_user_name, git_user_email]):
            return cls(
                git_sha=git_sha,
                git_user_name=git_user_name,
                git_user_email=git_user_email,
            )
        else:
            return None


class InputMeta(StrictModel):
    """Stores information about an input provided to generate the lockfile."""

    md5: Optional[str] = Field(..., description="md5 checksum for an input file")
    sha256: Optional[str] = Field(..., description="md5 checksum for an input file")

    @classmethod
    def create(
        cls, metadata_choices: AbstractSet[MetadataOption], src_file: pathlib.Path
    ) -> "InputMeta":
        if MetadataOption.InputSha in metadata_choices:
            sha256 = cls.get_input_sha256(src_file=src_file)
        else:
            sha256 = None
        if MetadataOption.InputMd5 in metadata_choices:
            md5 = cls.get_input_md5(src_file=src_file)
        else:
            md5 = None
        return cls(
            md5=md5,
            sha256=sha256,
        )

    @classmethod
    def get_input_md5(cls, src_file: pathlib.Path) -> str:
        hasher = hashlib.md5()
        return cls.hash_file(src_file=src_file, hasher=hasher)

    @classmethod
    def get_input_sha256(cls, src_file: pathlib.Path) -> str:
        hasher = hashlib.sha256()
        return cls.hash_file(src_file=src_file, hasher=hasher)

    @staticmethod
    def hash_file(src_file: pathlib.Path, hasher: "_Hash") -> str:
        with src_file.open("r") as infile:
            hasher.update(infile.read().encode("utf-8"))
        return hasher.hexdigest()


class LockMeta(StrictModel):
    content_hash: Dict[str, str] = Field(
        ..., description="Hash of dependencies for each target platform"
    )
    channels: List[Channel] = Field(
        ..., description="Channels used to resolve dependencies"
    )
    platforms: List[str] = Field(..., description="Target platforms")
    sources: List[str] = Field(
        ...,
        description="paths to source files, relative to the parent directory of the lockfile",
    )
    time_metadata: Optional[TimeMeta] = Field(
        default=None, description="Metadata dealing with the time lockfile was created"
    )
    git_metadata: Optional[GitMeta] = Field(
        default=None,
        description=(
            "Metadata dealing with the git repo the lockfile was created in and the user that created it"
        ),
    )
    inputs_metadata: Optional[Dict[str, InputMeta]] = Field(
        default=None,
        description="Metadata dealing with the input files used to create the lockfile",
    )
    custom_metadata: Optional[Dict[str, str]] = Field(
        default=None,
        description="Custom metadata provided by the user to be added to the lockfile",
    )

    def __or__(self, other: "LockMeta") -> "LockMeta":
        """merge other into self"""
        if other is None:
            return self
        elif not isinstance(other, LockMeta):
            raise TypeError

        if self.inputs_metadata is None:
            new_inputs_metadata = other.inputs_metadata
        elif other.inputs_metadata is None:
            new_inputs_metadata = self.inputs_metadata
        else:
            new_inputs_metadata = self.inputs_metadata
            new_inputs_metadata.update(other.inputs_metadata)

        if self.custom_metadata is None:
            new_custom_metadata = other.custom_metadata
        elif other.custom_metadata is None:
            new_custom_metadata = self.custom_metadata
        else:
            new_custom_metadata = self.custom_metadata
            for key in other.custom_metadata:
                if key in new_custom_metadata:
                    logger.warning(
                        f"Custom metadata key {key} provided twice, overwriting original value"
                        + f"({new_custom_metadata[key]}) with new value "
                        + f"({other.custom_metadata[key]})"
                    )
            new_custom_metadata.update(other.custom_metadata)
        return LockMeta(
            content_hash={**self.content_hash, **other.content_hash},
            channels=self.channels,
            platforms=sorted(set(self.platforms).union(other.platforms)),
            sources=ordered_union([self.sources, other.sources]),
            time_metadata=other.time_metadata,
            git_metadata=other.git_metadata,
            inputs_metadata=new_inputs_metadata,
            custom_metadata=new_custom_metadata,
        )

    @validator("channels", pre=True, always=True)
    def ensure_channels(cls, v: List[Union[str, Channel]]) -> List[Channel]:
        res = []
        for e in v:
            if isinstance(e, str):
                res.append(Channel.from_string(e))
            else:
                res.append(e)
        return typing.cast(List[Channel], res)


class Lockfile(StrictModel):

    version: ClassVar[int] = 1

    package: List[LockedDependency]
    metadata: LockMeta

    def __or__(self, other: "Lockfile") -> "Lockfile":
        return other.__ror__(self)

    def __ror__(self, other: "Optional[Lockfile]") -> "Lockfile":
        """
        merge self into other
        """
        if other is None:
            return self
        elif not isinstance(other, Lockfile):
            raise TypeError

        assert self.metadata.channels == other.metadata.channels

        ours = {d.key(): d for d in self.package}
        theirs = {d.key(): d for d in other.package}

        # Pick ours preferentially
        package: List[LockedDependency] = []
        for key in sorted(set(ours.keys()).union(theirs.keys())):
            if key not in ours or key[-1] not in self.metadata.platforms:
                package.append(theirs[key])
            else:
                package.append(ours[key])

        # Resort the conda packages topologically
        final_package = self._toposort(package)
        return Lockfile(package=final_package, metadata=other.metadata | self.metadata)

    def toposort_inplace(self) -> None:
        self.package = self._toposort(self.package)

    @staticmethod
    def _toposort(
        package: List[LockedDependency], update: bool = False
    ) -> List[LockedDependency]:
        platforms = {d.platform for d in package}

        # Resort the conda packages topologically
        final_package: List[LockedDependency] = []
        for platform in sorted(platforms):
            from .._vendor.conda.common.toposort import toposort

            # Add the remaining non-conda packages in the order in which they appeared.
            # Order the pip packages topologically ordered (might be not 100% perfect if they depend on
            # other conda packages, but good enough
            for manager in ["conda", "pip"]:
                lookup = defaultdict(set)
                packages: Dict[str, LockedDependency] = {}

                for d in package:
                    if d.platform != platform:
                        continue

                    if d.manager != manager:
                        continue

                    lookup[d.name] = set(d.dependencies)
                    packages[d.name] = d

                ordered = toposort(lookup)
                for package_name in ordered:
                    # since we could have a pure dep in here, that does not have a package
                    # eg a pip package that depends on a conda package (the conda package will not be in this list)
                    dep = packages.get(package_name)
                    if dep is None:
                        continue
                    if dep.manager != manager:
                        continue
                    # skip virtual packages
                    if dep.manager == "conda" and dep.name.startswith("__"):
                        continue

                    final_package.append(dep)

        return final_package


class UpdateSpecification:
    def __init__(
        self,
        locked: Optional[List[LockedDependency]] = None,
        update: Optional[List[str]] = None,
    ):
        self.locked = locked or []
        self.update = update or []
