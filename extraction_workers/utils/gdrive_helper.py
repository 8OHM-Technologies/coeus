import os
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

try:
    from google.oauth2 import service_account
    from googleapiclient.http import MediaFileUpload

    HAS_GDRIVE_LIBS = True
except ImportError:
    HAS_GDRIVE_LIBS = False
    
    
def load_gdrive_credentials(extraction_params):
    if not HAS_GDRIVE_LIBS:
        raise ImportError(
            "Google client libraries (google-api-python-client, google-auth) are not installed."
        )

    scopes = ["https://www.googleapis.com/auth/drive"]

    creds_param = extraction_params.get("gdrive_credentials")
    if creds_param:
        if isinstance(creds_param, dict):
            logger.info(
                "Loading Google Drive credentials from dictionary in extraction_params."
            )
            return service_account.Credentials.from_service_account_info(
                creds_param, scopes=scopes
            )
        elif isinstance(creds_param, str):
            try:
                info = json.loads(creds_param)
                logger.info(
                    "Loading Google Drive credentials from JSON string in extraction_params."
                )
                return service_account.Credentials.from_service_account_info(
                    info, scopes=scopes
                )
            except json.JSONDecodeError:
                if os.path.exists(creds_param):
                    logger.info(
                        f"Loading Google Drive credentials from file path in extraction_params: {creds_param}"
                    )
                    return service_account.Credentials.from_service_account_file(
                        creds_param, scopes=scopes
                    )
                else:
                    logger.warning(
                        f"Credentials path in extraction_params does not exist: {creds_param}"
                    )

    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_path and os.path.exists(env_path):
        logger.info(
            f"Loading Google Drive credentials from GOOGLE_APPLICATION_CREDENTIALS: {env_path}"
        )
        return service_account.Credentials.from_service_account_file(
            env_path, scopes=scopes
        )

    search_paths = ["/app/data/gdrive_credentials.json"]
    for path in search_paths:
        if os.path.exists(path):
            logger.info(f"Loading Google Drive credentials from candidate path: {path}")
            return service_account.Credentials.from_service_account_file(
                path, scopes=scopes
            )

    raise FileNotFoundError("Google Drive credentials not found.")


def list_gdrive_files(service, folder_id):
    files = set()
    page_token = None
    while True:
        try:
            query = f"'{folder_id}' in parents and trashed = false"
            response = (
                service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name)",
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for f in response.get("files", []):
                files.add(f["name"])
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        except Exception as e:
            logger.error(f"Error listing Google Drive files: {e}")
            raise
    return files


def upload_file_to_gdrive(service, file_path, folder_id):
    file_name = os.path.basename(file_path)
    logger.info(f"Uploading {file_name} to Google Drive...")

    if file_path.endswith(".pdf"):
        mime_type = "application/pdf"
    elif file_path.endswith(".json"):
        mime_type = "application/json"
    elif file_path.endswith(".html"):
        mime_type = "text/html"
    else:
        mime_type = "application/octet-stream"

    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

    try:
        file_obj = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        logger.info(
            f"Successfully uploaded {file_name} to Google Drive (ID: {file_obj.get('id')})"
        )
        return True
    except Exception as e:
        logger.error(f"Error uploading {file_name} to Google Drive: {e}")
        return False


# ---------------------------------------------------------------------------
# High-level GDrive Backup Manager
# ---------------------------------------------------------------------------
class GDriveBackupManager:
    """Wraps the full Google Drive init → list → upload → delete-local cycle.

    Usage::

        mgr = GDriveBackupManager.from_config(extraction_params)
        if mgr:
            existing = mgr.load_existing_files(local_output_dir)
            # ... scrape loop ...
            mgr.backup_files(["/path/to/data.json"])
    """

    def __init__(self, service, folder_id: str, delete_local: bool = False):
        self.service = service
        self.folder_id = folder_id
        self.delete_local = delete_local

    # -- Factory ----------------------------------------------------------

    @classmethod
    def from_config(cls, extraction_params: dict) -> "GDriveBackupManager | None":
        """Create from pipeline extraction_params, or return ``None``."""
        folder_id = extraction_params.get("gdrive_folder_id")
        if not folder_id:
            logger.info("Google Drive integration not enabled (no gdrive_folder_id).")
            return None

        logger.info("☁️  Initialising Google Drive backup manager...")
        try:
            from googleapiclient.discovery import build as _build

            creds = load_gdrive_credentials(extraction_params)
            service = _build("drive", "v3", credentials=creds)
        except Exception as e:
            logger.error(f"Failed to initialise Google Drive: {e}")
            raise

        delete_local = extraction_params.get("gdrive_delete_local", False)
        return cls(service, folder_id, delete_local=delete_local)

    # -- File listing -----------------------------------------------------

    def load_existing_files(self, local_output_dir: str | None = None) -> set[str]:
        """Return a set of filenames already stored (GDrive or local).

        When GDrive is active the remote listing is used; otherwise falls
        back to scanning *local_output_dir* for ``.html`` files.
        """
        try:
            remote_files = list_gdrive_files(self.service, self.folder_id)
            logger.info(f"Found {len(remote_files)} existing files on Google Drive.")
            return remote_files
        except Exception as e:
            logger.warning(f"Could not list GDrive files: {e}")
            if local_output_dir:
                return self._list_local(local_output_dir)
            return set()

    @staticmethod
    def _list_local(output_dir: str) -> set[str]:
        files = set()
        if os.path.isdir(output_dir):
            for fname in os.listdir(output_dir):
                if fname.endswith(".html"):
                    files.add(fname)
        logger.info(f"Found {len(files)} existing local HTML files.")
        return files

    # -- Upload / backup --------------------------------------------------

    def backup_files(self, file_paths: list[str]) -> None:
        """Upload a list of local files to Google Drive.

        If ``self.delete_local`` is ``True``, each file is removed from the
        local filesystem after a successful upload.
        """
        for fpath in file_paths:
            fname = os.path.basename(fpath)
            if not os.path.exists(fpath):
                logger.warning(f"  ⚠️ File does not exist, skipping upload: {fname}")
                continue
            if upload_file_to_gdrive(self.service, fpath, self.folder_id):
                logger.info(f"  ✅ Uploaded {fname} to Google Drive.")
                if self.delete_local:
                    try:
                        os.remove(fpath)
                        logger.info(f"  🗑️  Deleted local file: {fname}")
                    except Exception as e:
                        logger.warning(f"  Failed to delete local file {fname}: {e}")
            else:
                logger.warning(f"  ⚠️ Failed to upload {fname} to Google Drive.")

    def upload_file(self, file_path: str) -> bool:
        """Upload a single file. Returns ``True`` on success.

        Handles ``delete_local`` automatically.
        """
        fname = os.path.basename(file_path)
        ok = upload_file_to_gdrive(self.service, file_path, self.folder_id)
        if ok and self.delete_local:
            try:
                os.remove(file_path)
                logger.info(f"  🗑️  Deleted local file: {fname}")
            except Exception as e:
                logger.warning(f"  Failed to delete local file {fname}: {e}")
        return ok