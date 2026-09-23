import logging
import re
import asyncio
from typing import Optional
from aiogoogle import Aiogoogle

logger = logging.getLogger(__name__)

def _sanitize_folder_id(folder_id: str) -> str:
    """Validate Drive folder ID to prevent query injection."""
    if not folder_id or not re.fullmatch(r"[A-Za-z0-9_-]{10,}", folder_id):
        raise ValueError(f"Invalid Drive folder ID: {folder_id[:20]!r}")
    return folder_id


class DriveFoldersMixin:
    # --- internal helpers that reuse existing drive/aiogoogle to avoid discovery ---
    async def _list_subfolders_inner(self, drive, aiogoogle, parent_folder_id: str) -> list[dict]:
        folders = []
        page_token = None
        while True:
            request = drive.files.list(
                q=f"'{parent_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            )
            response = await self._execute_with_retry(aiogoogle, request)
            folders.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return folders

    async def _list_all_items_inner(self, drive, aiogoogle, parent_folder_id: str) -> list[dict]:
        items = []
        page_token = None
        while True:
            request = drive.files.list(
                q=f"'{parent_folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(id, name, mimeType)",
                pageToken=page_token,
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            )
            response = await self._execute_with_retry(aiogoogle, request)
            items.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return items

    async def list_subfolders(self, parent_folder_id: str) -> list[dict]:
        """List all subfolders in a Drive folder."""
        if not self._creds:
            await self._init_creds()
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            return await self._list_subfolders_inner(drive, aiogoogle, parent_folder_id)
    
    async def list_files_in_folder(self, folder_id: str) -> list[dict]:
        """List all non-folder files in a Drive folder (with size for validation)."""
        if not self._creds:
            await self._init_creds()
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            return await self._list_files_inner(drive, aiogoogle, folder_id)

    async def has_files(self, folder_id: str) -> bool:
        """Check if a folder contains any non-folder files."""
        if not self._creds:
            await self._init_creds()
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            return await self._has_files_inner(drive, aiogoogle, folder_id)

    async def list_all_items(self, parent_folder_id: str) -> list[dict]:
        """List all files and folders in a Drive folder."""
        if not self._creds:
            await self._init_creds()
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            return await self._list_all_items_inner(drive, aiogoogle, parent_folder_id)

    async def _folder_exists(self, folder_name: str, parent_id: str) -> str | None:
        """Check if a folder with given name already exists under parent (for idempotency)."""
        try:
            subfolders = await self.list_subfolders(parent_id)
            for f in subfolders:
                if f["name"] == folder_name:
                    return f["id"]
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        return None

    async def create_folder(self, folder_name: str, parent_id: str) -> str:
        """Create a folder in Drive and return its ID (idempotent — returns existing if found)."""
        existing_id = await self._folder_exists(folder_name, parent_id)
        if existing_id:
            logger.info(f"Folder '{folder_name}' already exists under {parent_id}, reusing {existing_id}")
            return existing_id
        if not self._creds:
            await self._init_creds()
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            request = drive.files.create(
                json={
                    "name": folder_name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id]
                },
                fields="id",
                supportsAllDrives=True
            )
            response = await self._execute_with_retry(aiogoogle, request)
            return response["id"]

    async def create_document(self, doc_name: str, parent_id: str) -> str:
        """Create a Google Doc in Drive and return its ID."""
        if not self._creds:
            await self._init_creds()
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            request = drive.files.create(
                json={
                    "name": doc_name,
                    "mimeType": "application/vnd.google-apps.document",
                    "parents": [parent_id]
                },
                fields="id",
                supportsAllDrives=True
            )
            response = await self._execute_with_retry(aiogoogle, request)
            return response["id"]
    
    async def copy_file_by_name(self, file_name: str, source_parent_id: str, target_parent_id: str) -> str | None:
        """Find a file by name in source and copy it to target."""
        if not self._creds:
            await self._init_creds()
        _sanitize_folder_id(source_parent_id)
        _sanitize_folder_id(target_parent_id)
        safe_name = _escape_query_value(file_name)
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            request = drive.files.list(
                q=f"'{source_parent_id}' in parents and name='{safe_name}' and trashed=false",
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            )
            response = await self._execute_with_retry(aiogoogle, request)
            files = response.get("files", [])
            if not files:
                return None
            source_file_id = files[0]["id"]
            copy_request = drive.files.copy(
                fileId=source_file_id,
                json={
                    "name": file_name,
                    "parents": [target_parent_id]
                },
                fields="id",
                supportsAllDrives=True
            )
            copy_response = await self._execute_with_retry(aiogoogle, copy_request)
            return copy_response["id"]

    async def grant_folder_access(self, folder_id: str, email: str, role: str = "writer") -> bool:
        """Grant Drive folder access to a user's Gmail (writer=editor can upload)."""
        if not email or "@" not in email:
            logger.warning(f"grant_folder_access: invalid email {email!r}")
            return False
        try:
            _sanitize_folder_id(folder_id)
        except ValueError as e:
            logger.warning(f"grant_folder_access: invalid folder {e}")
            return False
        if role not in ("reader", "writer", "commenter"):
            role = "writer"
        if not self._creds:
            await self._init_creds()
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            # Check existing permission to avoid duplicate (case-insensitive)
            try:
                perm_list = await self._execute_with_retry(
                    aiogoogle,
                    drive.permissions.list(
                        fileId=folder_id,
                        fields="permissions(id, emailAddress, role, type)",
                        supportsAllDrives=True,
                    ),
                )
                for p in perm_list.get("permissions", []):
                    if p.get("emailAddress", "").lower() == email.lower():
                        # Already has access — upgrade role if needed
                        if p.get("role") == role:
                            logger.info(f"User {email} already has {role} on {folder_id}")
                            return True
                        # Update role
                        try:
                            await self._execute_with_retry(
                                aiogoogle,
                                drive.permissions.update(
                                    fileId=folder_id,
                                    permissionId=p["id"],
                                    json={"role": role},
                                    supportsAllDrives=True,
                                ),
                            )
                            logger.info(f"Updated {email} role to {role} on {folder_id}")
                            return True
                        except Exception as e:
                            import logging
                            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                        return True
            except Exception as e:
                logger.debug(f"Permission list failed for {folder_id}: {e}")
            # Create new permission
            try:
                await self._execute_with_retry(
                    aiogoogle,
                    drive.permissions.create(
                        fileId=folder_id,
                        json={
                            "type": "user",
                            "role": role,
                            "emailAddress": email,
                        },
                        sendNotificationEmail=False,
                        supportsAllDrives=True,
                    ),
                )
                logger.info(f"Granted {role} on {folder_id} to {email}")
                return True
            except Exception as e:
                # Common errors: 404 folder not found, 403 sharing not allowed, 400 invalid email
                logger.warning(f"Failed to grant {email} on {folder_id}: {e}")
                return False

    async def revoke_folder_access(self, folder_id: str, email: str) -> bool:
        """Revoke Drive folder access for a user's Gmail."""
        if not email or "@" not in email:
            return False
        try:
            _sanitize_folder_id(folder_id)
        except ValueError:
            return False
        if not self._creds:
            await self._init_creds()
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            try:
                perm_list = await self._execute_with_retry(
                    aiogoogle,
                    drive.permissions.list(
                        fileId=folder_id,
                        fields="permissions(id, emailAddress)",
                        supportsAllDrives=True,
                    ),
                )
                target_id = None
                for p in perm_list.get("permissions", []):
                    if p.get("emailAddress", "").lower() == email.lower():
                        target_id = p["id"]
                        break
                if not target_id:
                    logger.debug(f"No permission for {email} on {folder_id} to revoke")
                    return True  # Already no access
                await self._execute_with_retry(
                    aiogoogle,
                    drive.permissions.delete(
                        fileId=folder_id,
                        permissionId=target_id,
                        supportsAllDrives=True,
                    ),
                )
                logger.info(f"Revoked {email} on {folder_id}")
                return True
            except Exception as e:
                if "cannotdeletepermission" in str(e).lower():
                    logger.debug(f"Could not revoke {email} on {folder_id} because permission is inherited.")
                    return True
                logger.warning(f"Failed to revoke {email} on {folder_id}: {e}")
                return False

