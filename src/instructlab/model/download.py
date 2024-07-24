# SPDX-License-Identifier: Apache-2.0

# Standard
from pathlib import Path
import abc
import logging
import os
import re
import subprocess

# Third Party
from huggingface_hub import hf_hub_download, list_repo_files
from huggingface_hub import logging as hf_logging
from huggingface_hub import snapshot_download
from packaging import version
import click

# First Party
from instructlab import clickext
from instructlab.configuration import DEFAULTS
from instructlab.utils import _extract_SHA, _load_json, is_huggingface_repo, is_oci_repo

logger = logging.getLogger(__name__)


class Downloader(abc.ABC):
    """Base class for a downloading backend"""

    def __init__(
        self,
        repository: str,
        release: str,
        download_dest: str,
    ) -> None:
        self.repository = repository
        self.release = release
        self.download_dest = download_dest

    @abc.abstractmethod
    def download(self) -> None:
        """Downloads model from specified repo/release and stores it into download_dest"""


class HFDownloader(Downloader):
    """Class to handle downloading safetensors and GGUF models from Hugging Face"""

    def __init__(
        self,
        repository: str,
        release: str,
        download_dest: str,
        filename: str,
        hf_token: str,
        ctx,
    ) -> None:
        super().__init__(
            repository=repository, release=release, download_dest=download_dest
        )
        self.filename = filename
        self.hf_token = hf_token
        self.ctx = ctx

    def download(self):
        """
        Download specified model from Hugging Face
        """
        click.echo(
            f"Downloading model from Hugging Face : {self.repository}@{self.release} to {self.download_dest}..."
        )

        if self.hf_token == "" and "instructlab" not in self.repository:
            raise ValueError(
                """HF_TOKEN var needs to be set in your environment to download HF Model.
                Alternatively, the token can be passed with --hf-token flag.
                The HF Token is used to authenticate your identity to the Hugging Face Hub."""
            )

        try:
            if self.ctx.obj is not None:
                hf_logging.set_verbosity(self.ctx.obj.config.general.log_level.upper())
            files = list_repo_files(repo_id=self.repository, token=self.hf_token)
            if any(".safetensors" in fname for fname in files):
                self.download_safetensors()
            else:
                self.download_gguf()

        except Exception as exc:
            click.secho(
                f"Downloading model failed with the following Hugging Face Hub error: {exc}",
                fg="red",
            )
            raise click.exceptions.Exit(1)

    def download_gguf(self) -> None:
        try:
            hf_hub_download(
                token=self.hf_token,
                repo_id=self.repository,
                revision=self.release,
                filename=self.filename,
                local_dir=self.download_dest,
            )

        except Exception as exc:
            click.secho(
                f"Downloading GGUF model failed with the following Hugging Face  Hub error: {exc}",
                fg="red",
            )
            raise click.exceptions.Exit(1)

    def download_safetensors(self) -> None:
        try:
            os.makedirs(
                name=os.path.join(self.download_dest, self.repository),
                exist_ok=True,
            )

            snapshot_download(
                token=self.hf_token,
                repo_id=self.repository,
                revision=self.release,
                local_dir=os.path.join(self.download_dest, self.repository),
            )
        except Exception as exc:
            click.secho(
                f"Downloading safetensors model failed with the following Hugging Face  Hub error: {exc}",
                fg="red",
            )
            raise click.exceptions.Exit(1)


class OCIDownloader(Downloader):
    """
    Class to handle downloading safetensors models from OCI Registries
    We are leveraging OCI v1.1 for this functionality
    """

    def __init__(self, repository: str, release: str, download_dest: str, ctx) -> None:
        super().__init__(
            repository=repository, release=release, download_dest=download_dest
        )
        self.ctx = ctx

    def _build_oci_model_file_map(self, oci_model_path: str) -> dict:
        """
        Helper function to build a mapping between blob files and what they represent
        Format for the index.json file can be found here: https://github.com/opencontainers/image-spec/blob/main/image-layout.md#indexjson-file
        """
        index_hash = ""
        index_ref_path = f"{oci_model_path}/index.json"
        try:
            index_ref = _load_json(Path(index_ref_path))
            match = None
            for manifest in index_ref["manifests"]:
                if (
                    manifest["mediaType"]
                    == "application/vnd.oci.image.manifest.v1+json"
                ):
                    match = _extract_SHA(manifest["digest"])

            if match:
                index_hash = match.group(1)
            else:
                raise ValueError(
                    f"could not find hash for index file at: {oci_model_path}"
                )
        except Exception as exc:
            raise exc

        blob_dir = f"{oci_model_path}/blobs/sha256"
        try:
            index = _load_json(Path(f"{blob_dir}/{index_hash}"))
        except Exception as exc:
            raise exc

        title_ref = "org.opencontainers.image.title"
        oci_model_file_map = {}
        try:
            for layer in index["layers"]:
                match = _extract_SHA(layer["digest"])

                if match:
                    blob_name = match.group(1)
                    oci_model_file_map[blob_name] = layer["annotations"][title_ref]
        except Exception as exc:
            raise ValueError(
                f"failed to build OCI model file mapping from: {blob_dir}/{index_hash}"
            ) from exc

        return oci_model_file_map

    def download(self):
        click.echo(
            f"Downloading model from OCI registry: {self.repository}@{self.release} to {self.download_dest}..."
        )

        model_name = self.repository.split("/")[-1]
        os.makedirs(os.path.join(self.download_dest, model_name), exist_ok=True)
        oci_dir = f"{DEFAULTS.OCI_DIR}/{model_name}"
        os.makedirs(oci_dir, exist_ok=True)

        # Check if skopeo is installed and the version is at least 1.15
        check_skopeo_version()

        command = [
            "skopeo",
            "copy",
            f"{self.repository}:{self.release}",
            f"oci:{oci_dir}",
        ]
        if (
            self.ctx.obj is not None
            and self.ctx.obj.config.general.log_level == logging.DEBUG
        ):
            command.append("--debug")

        try:
            subprocess.run(command, check=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                "skopeo not installed, but required to perform downloads from OCI registries. Exiting",
            ) from exc
        except Exception as e:
            click.secho(
                f"unexpected error: {e}",
                fg="red",
            )
            raise click.exceptions.Exit(1)

        file_map = self._build_oci_model_file_map(oci_dir)

        blob_dir = f"{oci_dir}/blobs/sha256/"
        for _, _, files in os.walk(blob_dir):
            for name in files:
                if name not in file_map:
                    continue
                dest = file_map[name]
                dest_model_path = os.path.join(self.download_dest, model_name, dest)
                # unlink any existing version of the file
                if os.path.exists(dest_model_path):
                    os.unlink(dest_model_path)

                # create symlink to files in cache, to avoid redownloading if model has been downloaded before
                os.symlink(
                    os.path.join(blob_dir, name),
                    dest_model_path,
                )


@click.command()
@click.option(
    "--repository",
    default=DEFAULTS.MERLINITE_GGUF_REPO,  # TODO: add to config.yaml
    show_default=True,
    help="Hugging Face  or OCI repository of the model to download.",
)
@click.option(
    "--release",
    default="main",  # TODO: add to config.yaml
    show_default=True,
    help="The revision of the model to download - e.g. a branch, tag, or commit hash for Hugging Face  repositories and tag or commit hash for OCI repositories.",
)
@click.option(
    "--filename",
    default=DEFAULTS.GGUF_MODEL_NAME,
    show_default="The default model location in the instructlab data directory.",
    help="Name of the model file to download from the Hugging Face repository.",
)
@click.option(
    "--model-dir",
    default=lambda: DEFAULTS.MODELS_DIR,
    show_default="The default system model location store, located in the data directory.",
    help="The local directory to download the model files into.",
)
@click.option(
    "--hf-token",
    default="",
    envvar="HF_TOKEN",
    help="User access token for connecting to the Hugging Face Hub.",
)
@click.pass_context
@clickext.display_params
def download(ctx, repository, release, filename, model_dir, hf_token):
    downloader = None

    if is_oci_repo(repository):
        downloader = OCIDownloader(
            repository=repository, release=release, download_dest=model_dir, ctx=ctx
        )
    elif is_huggingface_repo(repository):
        downloader = HFDownloader(
            repository=repository,
            release=release,
            download_dest=model_dir,
            filename=filename,
            hf_token=hf_token,
            ctx=ctx,
        )
    else:
        click.secho(
            f"repository {repository} matches neither Hugging Face nor OCI registry format. Please supply a valid repository",
            fg="red",
        )
        raise click.exceptions.Exit(1)

    try:
        downloader.download()
    except FileNotFoundError as exc:
        click.secho(
            "skopeo is not installed, please install recommended version 1.15",
            fg="red",
        )
        raise click.exceptions.Exit(1) from exc
    except Exception as exc:
        click.secho(
            f"Downloading model failed with the following error: {exc}",
            fg="red",
        )
        raise click.exceptions.Exit(1)


def check_skopeo_version():
    """
    Check if skopeo is installed and the version is at least 1.15
    This is required for downloading models from OCI registries.
    The function intentionally does not raise an exception if the version is lower than 1.15, other
    versions might work as well, but it is recommended to use at least 1.15.0
    """
    # Run the 'skopeo --version' command and capture the output
    result = subprocess.run(
        ["skopeo", "--version"], capture_output=True, text=True, check=True
    )
    logger.debug(f"'skopeo --version' output: {result.stdout}")

    # Extract the version number using a regular expression
    match = re.search(r"skopeo version (\d+\.\d+\.\d+)", result.stdout)
    if match:
        installed_version = match.group(1)
        logger.debug(f"detected skopeo version: {installed_version}")

        # Compare the extracted version with the required version
        if version.parse(installed_version) < version.parse("1.15.0"):
            logger.error(
                f"skopeo version {installed_version} is lower than 1.15. Consider upgrading. Downloading the model might fail."
            )
    else:
        logger.error(
            "Failed to determine skopeo version. Recommended version is 1.15. Downloading the model might fail."
        )
