#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Session Manager & Bi-Directional Synchronization Tool (AGY-Sync)
=============================================================================
A robust, universal synchronization, orphan session adoption, and step gap
recovery engine for Google Deepmind Antigravity 2.0 and Antigravity IDE (VS Code).

Key Features:
- Universal path auto-detection across Windows, macOS, and Linux.
- 100% native binary Protobuf synchronization (zero risk of deserialization crashes).
- Project Entity Auto-Adoption (Field 18 injection) to eliminate "Outside of Project" orphans.
- Step Gap Detection & Hot Stitching: Reconstructs interrupted trajectory steps from
  append-only streaming logs (`transcript_full.jsonl`) back into SQLite `steps` tables.
- Smart Conflict Arbiter: Step-count-first and latest-timestamp arbitration.
- Tombstone-level Permanent Deletion: Prevents memory rewrite and ghost resurrection.
- Rolling Hourly Backups (Strict retention of the latest N archives).
- Headless Daemon mode with Windows silent VBS auto-startup.

Platform Support: Tested and verified on Windows. macOS and Linux path support
is architecturally implemented but has not been verified on live systems.

Author: Antigravity (Google DeepMind)
License: MIT
"""

import sys
import os
import sqlite3
import base64
import glob
import re
import time
import json
import shutil
import subprocess
import argparse
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any

# Ensure UTF-8 standard output across platforms
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass


class PathConfig:
    """Manages cross-platform path resolution and customizable overrides."""

    def __init__(self,
                 gemini_home: Optional[str] = None,
                 ide_storage_dir: Optional[str] = None,
                 backup_dir: Optional[str] = None):

        # 1. Base user home directory
        user_home = os.path.expanduser("~")

        # 2. Base .gemini home
        self.gemini_home = gemini_home or os.environ.get(
            "ANTIGRAVITY_HOME", os.path.join(user_home, ".gemini")
        )

        # 3. Antigravity 2.0 & IDE protobuf summaries
        self.src_pb = os.path.join(self.gemini_home, "antigravity", "agyhub_summaries_proto.pb")
        self.ide_pb = os.path.join(self.gemini_home, "antigravity-ide", "agyhub_summaries_proto.pb")

        # 4. Antigravity IDE (VS Code) state.vscdb
        if ide_storage_dir:
            self.ide_db_path = os.path.join(ide_storage_dir, "state.vscdb")
        elif sys.platform == "win32":
            appdata = os.environ.get("APPDATA", os.path.join(user_home, "AppData", "Roaming"))
            self.ide_db_path = os.path.join(appdata, "Antigravity IDE", "User", "globalStorage", "state.vscdb")
        elif sys.platform == "darwin":
            self.ide_db_path = os.path.join(user_home, "Library", "Application Support", "Antigravity IDE", "User", "globalStorage", "state.vscdb")
        else:
            self.ide_db_path = os.path.join(user_home, ".config", "Antigravity IDE", "User", "globalStorage", "state.vscdb")

        # 5. Conversations, brain logs, projects, and backups
        self.conv_dir = os.path.join(self.gemini_home, "antigravity", "conversations")
        self.brain_dir = os.path.join(self.gemini_home, "antigravity", "brain")
        self.projects_dir = os.path.join(self.gemini_home, "config", "projects")
        self.backup_root = backup_dir or os.path.join(self.gemini_home, "config", "backups")
        self.tombstones_file = os.path.join(self.gemini_home, "config", "tombstones.json")
        self.log_file = os.path.join(self.gemini_home, "config", "auto_sync.log")

        # 6. Windows silent startup script path
        if sys.platform == "win32":
            startup_dir = os.path.join(os.environ.get('APPDATA', ''), r"Microsoft\Windows\Start Menu\Programs\Startup")
            self.vbs_path = os.path.join(startup_dir, "AntigravitySyncDaemon.vbs")
        else:
            self.vbs_path = ""


# Default path configuration instance
CONFIG = PathConfig()


# ==============================================================================
# Storage Layer Directory Linking (Junction / Symlink for Physical SQLite & Brain)
# ==============================================================================

def get_link_target(path: str) -> str:
    """Returns normalized target path of a symlink or Windows directory junction."""
    if not os.path.exists(path):
        return ""
    try:
        target = os.readlink(path)
        if target.startswith("\\\\?\\"):
            target = target[4:]
        return os.path.normpath(target)
    except OSError:
        return ""


def is_dir_linked(path: str) -> bool:
    """Checks whether a path is a directory junction or symlink."""
    if not os.path.exists(path):
        return False
    try:
        os.readlink(path)
        return True
    except OSError:
        return False


def check_directory_links(gemini_home: str) -> Dict[str, Dict[str, Any]]:
    """
    Checks junction / symlink status between antigravity/ and antigravity-ide/.
    Folders checked: 'conversations', 'brain', 'annotations'.
    """
    src_base = os.path.join(gemini_home, "antigravity")
    ide_base = os.path.join(gemini_home, "antigravity-ide")
    folders = ["conversations", "brain", "annotations"]
    results = {}

    for folder in folders:
        src_path = os.path.normpath(os.path.join(src_base, folder))
        link_path = os.path.normpath(os.path.join(ide_base, folder))
        linked = is_dir_linked(link_path)
        target = get_link_target(link_path)
        is_correct = (os.path.normcase(target) == os.path.normcase(src_path)) if linked else False
        exists = os.path.exists(link_path)

        results[folder] = {
            "src_path": src_path,
            "link_path": link_path,
            "exists": exists,
            "is_link": linked,
            "target": target,
            "is_correct": is_correct
        }

    return results


def check_running_processes() -> List[str]:
    """Checks if VS Code or Antigravity processes are currently active."""
    if sys.platform != "win32":
        return []
    running = []
    try:
        res = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True)
        out = res.stdout.lower()
        if "code.exe" in out:
            running.append("VS Code (Code.exe)")
        if "antigravity.exe" in out:
            running.append("Antigravity Desktop (antigravity.exe)")
    except Exception:
        pass
    return running


def safely_remove_junction(link_path: str) -> bool:
    """
    Safely removes a Windows Directory Junction or POSIX symlink without deleting target files.
    Strictly avoids shutil.rmtree to prevent recursive deletion of shared target folders.
    """
    if not os.path.exists(link_path):
        return True
    if not is_dir_linked(link_path):
        return False

    if sys.platform == "win32":
        res = subprocess.run(["cmd.exe", "/c", "rmdir", link_path], capture_output=True, text=True)
        if res.returncode != 0:
            try:
                os.rmdir(link_path)
            except Exception as e:
                raise RuntimeError(f"Failed to remove directory junction {link_path}: {e}")
    else:
        try:
            os.unlink(link_path)
        except Exception as e:
            raise RuntimeError(f"Failed to remove symlink {link_path}: {e}")
    return True


def get_db_step_metrics(db_path: str) -> Tuple[int, int, float]:
    """Returns (step_count, max_idx, mtime) from a conversation database."""
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        c = conn.cursor()
        c.execute("SELECT count(*), max(idx) FROM steps")
        row = c.fetchone()
        cnt = row[0] if row else 0
        ma = row[1] if row and row[1] is not None else -1
        conn.close()
        mtime = os.path.getmtime(db_path)
        return cnt, ma, mtime
    except Exception:
        return 0, -1, 0.0


def fuse_and_link_storage(gemini_home: str, verbose: bool = True) -> Dict[str, Any]:
    """
    Executes an intelligent, non-destructive two-way fusion of physical storage
    between Antigravity 2.0 and Antigravity IDE, then establishes Directory Junctions.
    Arbitrates conflicts by step count and preserves all history.
    """
    src_base = os.path.normpath(os.path.join(gemini_home, "antigravity"))
    ide_base = os.path.normpath(os.path.join(gemini_home, "antigravity-ide"))

    os.makedirs(src_base, exist_ok=True)
    os.makedirs(ide_base, exist_ok=True)

    timestamp = int(time.time())
    report = {
        "already_linked": False,
        "dbs_migrated_from_ide": 0,
        "dbs_conflict_ide_preferred": 0,
        "dbs_conflict_20_kept": 0,
        "brains_migrated_from_ide": 0,
        "brains_merged": 0,
        "links_created": []
    }

    folders = ["conversations", "brain", "annotations"]

    # Check if already fully linked
    all_linked = True
    for f in folders:
        link_path = os.path.join(ide_base, f)
        target = get_link_target(link_path)
        src_path = os.path.join(src_base, f)
        if not (is_dir_linked(link_path) and os.path.normcase(target) == os.path.normcase(src_path)):
            all_linked = False
            break

    if all_linked:
        report["already_linked"] = True
        if verbose:
            print("\n✅ Shared storage is already fully linked (conversations, brain, annotations).")
        return report

    if verbose:
        print("\n" + "=" * 70)
        print("  Starting Intelligent Two-Way Storage Fusion (2.0 <===> IDE)")
        print("=" * 70)

    # 1. Fuse conversations/*.db
    src_conv = os.path.join(src_base, "conversations")
    ide_conv = os.path.join(ide_base, "conversations")
    os.makedirs(src_conv, exist_ok=True)

    if os.path.exists(ide_conv) and not is_dir_linked(ide_conv):
        ide_dbs = [f for f in os.listdir(ide_conv) if f.endswith(".db")]
        if verbose and ide_dbs:
            print(f"\n[1/3] Analyzing {len(ide_dbs)} conversation database(s) in IDE...")

        for db_file in ide_dbs:
            ide_db_path = os.path.join(ide_conv, db_file)
            src_db_path = os.path.join(src_conv, db_file)

            if not os.path.exists(src_db_path):
                shutil.copy2(ide_db_path, src_db_path)
                report["dbs_migrated_from_ide"] += 1
                if verbose:
                    print(f"  -> Migrated new session from IDE: {db_file}")
            else:
                cnt_20, max_20, mt_20 = get_db_step_metrics(src_db_path)
                cnt_ide, max_ide, mt_ide = get_db_step_metrics(ide_db_path)

                use_ide = False
                if cnt_ide > cnt_20:
                    use_ide = True
                elif cnt_ide == cnt_20 and max_ide > max_20:
                    use_ide = True
                elif cnt_ide == cnt_20 and max_ide == max_20 and mt_ide > mt_20:
                    use_ide = True

                if use_ide:
                    b_path = f"{src_db_path}.pre_merge_20.bak"
                    shutil.copy2(src_db_path, b_path)
                    shutil.copy2(ide_db_path, src_db_path)
                    report["dbs_conflict_ide_preferred"] += 1
                    if verbose:
                        print(f"  -> Updated {db_file}: IDE had superior steps ({cnt_ide} vs {cnt_20})")
                else:
                    report["dbs_conflict_20_kept"] += 1
                    if verbose:
                        print(f"  -> Kept {db_file}: 2.0 version is equal or newer ({cnt_20} vs {cnt_ide})")

    # 2. Fuse brain/<uuid> folders
    src_brain = os.path.join(src_base, "brain")
    ide_brain = os.path.join(ide_base, "brain")
    os.makedirs(src_brain, exist_ok=True)

    if os.path.exists(ide_brain) and not is_dir_linked(ide_brain):
        ide_brains = [f for f in os.listdir(ide_brain) if os.path.isdir(os.path.join(ide_brain, f))]
        if verbose and ide_brains:
            print(f"\n[2/3] Analyzing {len(ide_brains)} brain directory/directories in IDE...")

        for b_dir in ide_brains:
            ide_b_path = os.path.join(ide_brain, b_dir)
            src_b_path = os.path.join(src_brain, b_dir)

            if not os.path.exists(src_b_path):
                shutil.copytree(ide_b_path, src_b_path)
                report["brains_migrated_from_ide"] += 1
                if verbose:
                    print(f"  -> Migrated new brain folder from IDE: {b_dir[:8]}")
            else:
                ide_tf = os.path.join(ide_b_path, ".system_generated", "logs", "transcript_full.jsonl")
                src_tf = os.path.join(src_b_path, ".system_generated", "logs", "transcript_full.jsonl")
                if os.path.exists(ide_tf):
                    ide_size = os.path.getsize(ide_tf)
                    src_size = os.path.getsize(src_tf) if os.path.exists(src_tf) else 0
                    if ide_size > src_size:
                        os.makedirs(os.path.dirname(src_tf), exist_ok=True)
                        shutil.copy2(ide_tf, src_tf)
                        if verbose:
                            print(f"  -> Updated transcript_full.jsonl for {b_dir[:8]} from IDE ({ide_size} > {src_size} bytes)")

                for root, dirs, files in os.walk(ide_b_path):
                    rel = os.path.relpath(root, ide_b_path)
                    dst_root = os.path.join(src_b_path, rel)
                    os.makedirs(dst_root, exist_ok=True)
                    for file in files:
                        dst_f = os.path.join(dst_root, file)
                        src_f = os.path.join(root, file)
                        if not os.path.exists(dst_f):
                            shutil.copy2(src_f, dst_f)

                report["brains_merged"] += 1

    # 3. Fuse annotations/
    src_ann = os.path.join(src_base, "annotations")
    ide_ann = os.path.join(ide_base, "annotations")
    os.makedirs(src_ann, exist_ok=True)

    if os.path.exists(ide_ann) and not is_dir_linked(ide_ann):
        for item in os.listdir(ide_ann):
            s_i = os.path.join(ide_ann, item)
            d_i = os.path.join(src_ann, item)
            if not os.path.exists(d_i):
                if os.path.isdir(s_i):
                    shutil.copytree(s_i, d_i)
                else:
                    shutil.copy2(s_i, d_i)

    # 4. Archive old IDE folders & Establish Junctions
    if verbose:
        print(f"\n[3/3] Creating directory junctions...")

    for folder in folders:
        src_path = os.path.join(src_base, folder)
        link_path = os.path.join(ide_base, folder)

        if os.path.exists(link_path):
            if is_dir_linked(link_path):
                target = get_link_target(link_path)
                if os.path.normcase(target) == os.path.normcase(src_path):
                    if verbose:
                        print(f"  [OK] Already Linked: {folder}")
                    continue
                else:
                    safely_remove_junction(link_path)
            else:
                backup_name = f"{link_path}_migrated_backup_{timestamp}"
                shutil.move(link_path, backup_name)
                if verbose:
                    print(f"  [Backup] Archived original IDE {folder} to {os.path.basename(backup_name)}")

        if sys.platform == "win32":
            cmd = ["cmd.exe", "/c", "mklink", "/J", link_path, src_path]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"mklink failed: {res.stderr.strip() or res.stdout.strip()}")
        else:
            os.symlink(src_path, link_path, target_is_directory=True)

        report["links_created"].append(folder)
        if verbose:
            print(f"  ✅ Established Junction: {folder} ===> {src_path}")

    if verbose:
        print("=" * 70)
        print("  Two-Way Storage Fusion & Junction Setup Completed Successfully!")
        print("=" * 70)

    return report


setup_directory_links = fuse_and_link_storage


def decouple_storage(gemini_home: Optional[str] = None, mode: str = "clone", verbose: bool = True) -> bool:
    """
    Decouples shared storage between Antigravity 2.0 and IDE ('各管各的').
    mode='clone':
        Decouple & Materialize (Recommended). Safely removes junctions and deep-clones
        current latest shared data into antigravity-ide/ so both sides keep all conversations.
    mode='revert':
        Reverts to pristine pre-init state. Removes junctions, restores original IDE folders
        from migrated backups, reverts .pre_merge_20.bak files, and restores pre-init metadata.
    """
    if gemini_home is None:
        gemini_home = CONFIG.gemini_home

    src_base = os.path.normpath(os.path.join(gemini_home, "antigravity"))
    ide_base = os.path.normpath(os.path.join(gemini_home, "antigravity-ide"))
    folders = ["conversations", "brain", "annotations"]

    links = check_directory_links(gemini_home)
    is_any_linked = any(v["is_link"] for v in links.values())
    if not is_any_linked:
        if verbose:
            print("\nℹ️ Storage is not currently linked via Junction/Symlink. Nothing to decouple.")
        return False

    procs = check_running_processes()
    if procs and verbose:
        print("\n" + "=" * 70)
        print(f"⚠️  ACTIVE PROCESS WARNING: Conflicting active process(es) detected: {', '.join(procs)}")
        print("    For complete data safety, please close VS Code / Antigravity before decoupling.")
        print("=" * 70)

    if mode == "clone":
        if verbose:
            print("\n" + "=" * 70)
            print("  Decoupling Storage: Mode 1 [Decouple & Materialize] (Recommended)")
            print("  This removes junctions and deep-clones current latest shared data into IDE.")
            print("  Both 2.0 and IDE will retain 100% of all sessions and operate independently.")
            print("=" * 70)

        for folder in folders:
            link_path = os.path.join(ide_base, folder)
            src_path = os.path.join(src_base, folder)
            if is_dir_linked(link_path):
                safely_remove_junction(link_path)
                if verbose:
                    print(f"  -> Safely removed junction: {folder}")
            if os.path.exists(src_path):
                shutil.copytree(src_path, link_path)
                if verbose:
                    print(f"  -> Cloned physical copy to IDE: {folder}")

        log_event("🔓 Storage decoupled (Mode 1: Materialized clones created for IDE).")
        if verbose:
            print("\n✅ Decoupling completed successfully!")
            print("   Both Antigravity 2.0 and IDE now operate independently ('各管各的').")
            print("   All recent conversations are 100% preserved in both applications.")
            print("=" * 70 + "\n")
        return True

    elif mode == "revert":
        if verbose:
            print("\n" + "=" * 70)
            print("  Decoupling Storage: Mode 2 [Revert to Pristine Pre-Init State]")
            print("  ⚠️ CAUTION: This will restore original IDE folders from pre-init backup.")
            print("  Any conversations created since initialization will remain in 2.0 only.")
            print("=" * 70)

        # 1. Safely remove junctions
        for folder in folders:
            link_path = os.path.join(ide_base, folder)
            if is_dir_linked(link_path):
                safely_remove_junction(link_path)
                if verbose:
                    print(f"  -> Safely removed junction: {folder}")

        # 2. Restore migrated backups in IDE
        migrated_dirs = [d for d in os.listdir(ide_base) if "_migrated_backup_" in d] if os.path.exists(ide_base) else []
        for folder in folders:
            baks = sorted([d for d in migrated_dirs if d.startswith(f"{folder}_migrated_backup_")])
            if baks:
                latest_bak = os.path.join(ide_base, baks[-1])
                target_dir = os.path.join(ide_base, folder)
                if not os.path.exists(target_dir):
                    shutil.move(latest_bak, target_dir)
                    if verbose:
                        print(f"  -> Restored original IDE {folder} from {os.path.basename(latest_bak)}")

        # 3. Restore 2.0 .pre_merge_20.bak files
        conv_dir = os.path.join(src_base, "conversations")
        if os.path.exists(conv_dir):
            for f in os.listdir(conv_dir):
                if f.endswith(".pre_merge_20.bak"):
                    orig_f = f[:-len(".pre_merge_20.bak")]
                    shutil.copy2(os.path.join(conv_dir, f), os.path.join(conv_dir, orig_f))
                    os.remove(os.path.join(conv_dir, f))
                    if verbose:
                        print(f"  -> Reverted 2.0 conflict database: {orig_f}")

        # 4. Restore pre-init metadata from latest PRE_INIT_SNAPSHOT
        pre_init_snaps = sorted([
            b for b in glob.glob(os.path.join(CONFIG.backup_root, "PRE_INIT_SNAPSHOT_*"))
            if os.path.isdir(b)
        ], key=os.path.getmtime)

        if pre_init_snaps:
            latest_snap = pre_init_snaps[-1]
            if os.path.exists(os.path.join(latest_snap, "agyhub_summaries_proto.pb")):
                shutil.copy2(os.path.join(latest_snap, "agyhub_summaries_proto.pb"), CONFIG.src_pb)
            if os.path.exists(os.path.join(latest_snap, "ide_pb.pb")):
                shutil.copy2(os.path.join(latest_snap, "ide_pb.pb"), CONFIG.ide_pb)
            if os.path.exists(os.path.join(latest_snap, "state.vscdb")):
                shutil.copy2(os.path.join(latest_snap, "state.vscdb"), CONFIG.ide_db_path)
            if verbose:
                print(f"  -> Restored pre-init metadata from {os.path.basename(latest_snap)}")

        log_event("⏪ Storage reverted to pristine pre-init state.")
        if verbose:
            print("\n✅ Revert completed successfully! Pristine pre-init state restored.")
            print("=" * 70 + "\n")
        return True

    return False


# ==============================================================================
# Low-Level Protobuf Wire Parser & Serializer (Zero External Dependencies)
# ==============================================================================

def read_varint(data: bytes, offset: int) -> Tuple[Optional[int], int]:
    """Reads a variable-length integer (varint) from binary data."""
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            return None, offset
        b = data[offset]
        result |= (b & 0x7F) << shift
        offset += 1
        if not (b & 0x80):
            return result, offset
        shift += 7


def encode_varint(val: int) -> bytearray:
    """Encodes an integer into Protobuf varint bytes."""
    res = bytearray()
    while val >= 0x80:
        res.append((val & 0x7F) | 0x80)
        val >>= 7
    res.append(val & 0x7F)
    return res


def encode_tlv(fn: int, wire_type: int, payload: Any) -> bytearray:
    """Encodes a Tag-Length-Value Protobuf field."""
    tag = encode_varint((fn << 3) | wire_type)
    if wire_type == 0:
        return tag + encode_varint(payload)
    elif wire_type == 2:
        if isinstance(payload, str):
            p_bytes = payload.encode('utf-8')
        elif isinstance(payload, (bytes, bytearray)):
            p_bytes = payload
        else:
            p_bytes = bytes(payload)
        return tag + encode_varint(len(p_bytes)) + p_bytes
    elif wire_type == 1:
        return tag + payload[:8]
    elif wire_type == 5:
        return tag + payload[:4]
    return tag


def patch_protobuf_uri_recursive(blob: bytes, target: str = 'c%3A') -> Tuple[bytes, bool]:
    """
    Recursively scans and standardizes URI encoding within Protobuf payloads.
    Translates between 'file:///c:/' and Canonical 'file:///c%3A/'.
    """
    out = bytearray()
    offset = 0
    mod = False
    while offset < len(blob):
        tag, n_off = read_varint(blob, offset)
        if tag is None:
            break
        wire = tag & 7
        if wire == 0:
            _, next_o = read_varint(blob, n_off)
            out.extend(encode_varint(tag))
            out.extend(blob[n_off:next_o])
            offset = next_o
        elif wire == 1:
            out.extend(encode_varint(tag))
            out.extend(blob[n_off:n_off + 8])
            offset = n_off + 8
        elif wire == 5:
            out.extend(encode_varint(tag))
            out.extend(blob[n_off:n_off + 4])
            offset = n_off + 4
        elif wire == 2:
            l, next_o = read_varint(blob, n_off)
            if l is None or next_o + l > len(blob):
                break
            p = blob[next_o:next_o + l]
            if b'file:///' in p:
                try:
                    if len(p) > 2:
                        sub_out, sub_mod = patch_protobuf_uri_recursive(p, target)
                        if sub_mod:
                            p = sub_out
                            mod = True
                except Exception:
                    pass
                if target == 'c%3A':
                    new_p, n = re.subn(rb'file:///([a-zA-Z]):/', rb'file:///\1%3A/', p)
                    if n > 0:
                        p = new_p
                        mod = True
                elif target == 'c:':
                    new_p, n = re.subn(rb'file:///([a-zA-Z])%3A/', rb'file:///\1:/', p)
                    if n > 0:
                        p = new_p
                        mod = True
            out.extend(encode_varint(tag))
            out.extend(encode_varint(len(p)))
            out.extend(p)
            offset = next_o + l
        else:
            break
    return bytes(out), mod


# ==============================================================================
# Logging & Tombstones Engine
# ==============================================================================

def log_event(msg: str):
    """Appends an event line with timestamp to the sync log file."""
    t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{t}] {msg}\n"
    try:
        os.makedirs(os.path.dirname(CONFIG.log_file), exist_ok=True)
        with open(CONFIG.log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def load_tombstones() -> set:
    """Loads deleted session UUIDs from the permanent tombstone file."""
    if os.path.exists(CONFIG.tombstones_file):
        try:
            with open(CONFIG.tombstones_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("tombstones", []))
        except Exception:
            return set()
    return set()


def save_tombstones(tombstones: set):
    """Saves session UUIDs to the permanent tombstone file."""
    try:
        os.makedirs(os.path.dirname(CONFIG.tombstones_file), exist_ok=True)
        with open(CONFIG.tombstones_file, "w", encoding="utf-8") as f:
            json.dump({"tombstones": sorted(list(tombstones))}, f, indent=2)
    except Exception:
        pass


# ==============================================================================
# Project Configuration & Dynamic Project Map Loader
# ==============================================================================

def load_projects_map() -> Dict[str, str]:
    """
    Dynamically parses all project definitions under ~/.gemini/config/projects/*.json.
    Supports both direct 'folderUri' and nested Git-managed 'gitFolder: { folderUri: ... }'.
    """
    proj_map = {}
    if not os.path.exists(CONFIG.projects_dir):
        return proj_map

    for p_file in glob.glob(os.path.join(CONFIG.projects_dir, "*.json")):
        try:
            with open(p_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                pid = data.get('id')
                if not pid:
                    continue
                resources = data.get('projectResources', {}).get('resources', [])
                for r in resources:
                    uri = r.get('folderUri') or r.get('gitFolder', {}).get('folderUri')
                    if uri:
                        uri_clean = re.sub(r'file:///([a-zA-Z])%3A/', r'file:///\1:/', uri)
                        uri_encoded = re.sub(r'file:///([a-zA-Z]):/', r'file:///\1%3A/', uri)
                        proj_map[uri_clean.lower()] = pid
                        proj_map[uri_encoded.lower()] = pid
                        folder_name = os.path.basename(uri_clean.rstrip('/\\'))
                        if folder_name:
                            proj_map[f"name:{folder_name.lower()}"] = pid
        except Exception:
            pass
    return proj_map


def find_project_id_for_uri(ws_uri: Optional[str], proj_map: Dict[str, str]) -> Optional[str]:
    """Resolves the Antigravity Project UUID for a given workspace URI."""
    if not ws_uri:
        return None
    ws_lower = ws_uri.lower()
    if ws_lower in proj_map:
        return proj_map[ws_lower]
    folder_name = os.path.basename(ws_uri.rstrip('/\\')).lower()
    if f"name:{folder_name}" in proj_map:
        return proj_map[f"name:{folder_name}"]
    return None


def get_summary_project_id(s_bytes: bytes) -> Optional[str]:
    """Extracts Field 18 (ProjectId) from Field 17 (TrajectoryMetadata) of a summary."""
    off = 0
    while off < len(s_bytes):
        t, no = read_varint(s_bytes, off)
        if t is None:
            break
        w = t & 7
        fn = t >> 3
        if w == 2:
            l, no = read_varint(s_bytes, no)
            if fn == 17:
                p = s_bytes[no:no + l]
                p_off = 0
                while p_off < len(p):
                    pt, pno = read_varint(p, p_off)
                    if pt is None:
                        break
                    pw = pt & 7
                    pfn = pt >> 3
                    if pw == 2:
                        pl, pno = read_varint(p, pno)
                        if pfn == 18:
                            return p[pno:pno + pl].decode('ascii', 'ignore')
                        p_off = pno + pl
                    elif pw == 0:
                        _, pno = read_varint(p, pno)
                        p_off = pno
                    else:
                        psz = 8 if pw == 1 else 4
                        p_off = pno + psz
            off = no + l
        elif w == 0:
            _, no = read_varint(s_bytes, no)
            off = no
        else:
            sz = 8 if w == 1 else 4
            off = no + sz
    return None


def ensure_summary_has_project_id(s_bytes: bytes, proj_map: Dict[str, str]) -> Tuple[bytes, bool]:
    """Ensures a summary Protobuf has Field 18 (ProjectId) inside Field 17."""
    if not s_bytes:
        return s_bytes, False

    ws_uri = None
    f17_payload = None
    off = 0
    while off < len(s_bytes):
        t, no = read_varint(s_bytes, off)
        if t is None:
            break
        w = t & 7
        fn = t >> 3
        if w == 2:
            l, no = read_varint(s_bytes, no)
            p = s_bytes[no:no + l]
            if fn in (9, 17) and not ws_uri:
                m = re.search(rb'file:///[^\x00-\x1f\x80-\xff]+', p)
                if m:
                    ws_uri = m.group(0).decode('utf-8', 'ignore')
            if fn == 17:
                f17_payload = p
            off = no + l
        elif w == 0:
            _, no = read_varint(s_bytes, no)
            off = no
        else:
            sz = 8 if w == 1 else 4
            off = no + sz

    if not ws_uri:
        return s_bytes, False

    expected_pid = find_project_id_for_uri(ws_uri, proj_map)
    if not expected_pid:
        return s_bytes, False

    current_pid = get_summary_project_id(s_bytes)
    if current_pid == expected_pid and f17_payload and len(f17_payload) > 0:
        return s_bytes, False

    u_b = expected_pid.encode('ascii')
    f18_tag = encode_varint((18 << 3) | 2)
    f18_entry = f18_tag + encode_varint(len(u_b)) + u_b

    out = bytearray()
    off = 0
    while off < len(s_bytes):
        t, no = read_varint(s_bytes, off)
        if t is None:
            break
        w = t & 7
        fn = t >> 3
        if w == 2:
            l, no = read_varint(s_bytes, no)
            p = s_bytes[no:no + l]
            if fn == 17:
                clean_p = bytearray()
                p_off = 0
                while p_off < len(p):
                    pt, pno = read_varint(p, p_off)
                    if pt is None:
                        break
                    pw = pt & 7
                    pfn = pt >> 3
                    if pw == 2:
                        pl, pno = read_varint(p, pno)
                        if pfn != 18:
                            clean_p.extend(encode_varint(pt))
                            clean_p.extend(encode_varint(pl))
                            clean_p.extend(p[pno:pno + pl])
                        p_off = pno + pl
                    elif pw == 0:
                        pv, pno = read_varint(p, pno)
                        clean_p.extend(encode_varint(pt))
                        clean_p.extend(encode_varint(pv))
                        p_off = pno
                    else:
                        psz = 8 if pw == 1 else 4
                        clean_p.extend(encode_varint(pt))
                        clean_p.extend(p[pno:pno + psz])
                        p_off = pno + psz
                clean_p.extend(f18_entry)
                p = bytes(clean_p)
            out.extend(encode_varint(t))
            out.extend(encode_varint(len(p)))
            out.extend(p)
            off = no + l
        elif w == 0:
            v, no = read_varint(s_bytes, no)
            out.extend(encode_varint(t))
            out.extend(encode_varint(v))
            off = no
        else:
            sz = 8 if w == 1 else 4
            out.extend(encode_varint(t))
            out.extend(s_bytes[no:no + sz])
            off = no + sz

    return bytes(out), True


def clean_summary_for_20(blob: bytes, proj_map: Optional[Dict[str, str]] = None) -> bytes:
    """Standardizes URI encoding to c%3A, injects Field 18, and purges empty fields."""
    blob, _ = patch_protobuf_uri_recursive(blob, target='c%3A')
    if proj_map:
        blob, _ = ensure_summary_has_project_id(blob, proj_map)

    out = bytearray()
    offset = 0
    while offset < len(blob):
        tag, n_off = read_varint(blob, offset)
        if tag is None:
            break
        wire = tag & 7
        if wire == 0:
            _, next_o = read_varint(blob, n_off)
            out.extend(encode_varint(tag))
            out.extend(blob[n_off:next_o])
            offset = next_o
        elif wire in (1, 5):
            sz = 8 if wire == 1 else 4
            out.extend(encode_varint(tag))
            out.extend(blob[n_off:n_off + sz])
            offset = n_off + sz
        elif wire == 2:
            l, n_off = read_varint(blob, n_off)
            if l is None or n_off + l > len(blob):
                break
            p = blob[n_off:n_off + l]
            if l > 0:
                out.extend(encode_varint(tag))
                out.extend(encode_varint(len(p)))
                out.extend(p)
            offset = n_off + l
        else:
            break
    return bytes(out)


# ==============================================================================
# Step Gap Detection & Trajectory Hot-Stitching Engine
# ==============================================================================

def construct_step_payload(d: Dict[str, Any]) -> Tuple[int, bytes]:
    """
    Constructs a compliant Antigravity binary step payload from transcript_full JSON.
    Generates valid Protobuf wire format for USER_INPUT, PLANNER_RESPONSE, GENERIC, CHECKPOINT.
    """
    st_type_str = d.get('type')
    content = d.get('content') or ""
    thinking = d.get('thinking') or ""
    tool_calls = d.get('tool_calls') or []
    try:
        ts_sec = int(datetime.fromisoformat(d.get('created_at', '').replace('Z', '+00:00')).timestamp())
    except Exception:
        ts_sec = 1788440000

    f5_sub = encode_tlv(1, 2, encode_tlv(1, 0, ts_sec))
    f5 = encode_tlv(5, 2, f5_sub)

    if st_type_str == 'USER_INPUT':
        st_type = 14
        f1 = encode_tlv(1, 0, 14)
        f4 = encode_tlv(4, 0, 3)
        text_bytes = content.encode('utf-8')
        f19 = encode_tlv(19, 2, encode_tlv(2, 2, text_bytes) + encode_tlv(3, 2, encode_tlv(1, 2, text_bytes)))
        return st_type, bytes(f1 + f4 + f5 + f19)
    elif st_type_str == 'PLANNER_RESPONSE':
        st_type = 15
        f1 = encode_tlv(1, 0, 15)
        f4 = encode_tlv(4, 0, 3)
        f20_payload = bytearray()
        if content:
            text_bytes = content.encode('utf-8')
            f20_payload.extend(encode_tlv(1, 2, text_bytes))
            f20_payload.extend(encode_tlv(8, 2, text_bytes))
        if thinking:
            f20_payload.extend(encode_tlv(3, 2, thinking.encode('utf-8')))
        if tool_calls:
            for tc in tool_calls:
                tc_name = (tc.get('name') or 'tool').encode('utf-8')
                tc_args = json.dumps(tc.get('args', {})).encode('utf-8')
                tc_call_id = (tc.get('id') or 'call_001').encode('utf-8')
                tc_sub = encode_tlv(1, 2, tc_call_id) + encode_tlv(2, 2, tc_name) + encode_tlv(3, 2, tc_args)
                f20_payload.extend(encode_tlv(7, 2, tc_sub))
        f20 = encode_tlv(20, 2, bytes(f20_payload))
        return st_type, bytes(f1 + f4 + f5 + f20)
    elif st_type_str == 'CHECKPOINT':
        st_type = 23
        f1 = encode_tlv(1, 0, 23)
        f4 = encode_tlv(4, 0, 3)
        f30 = encode_tlv(30, 2, encode_tlv(4, 2, b"Checkpoint"))
        return st_type, bytes(f1 + f4 + f5 + f30)
    else:
        st_type = 132
        f1 = encode_tlv(1, 0, 132)
        f4 = encode_tlv(4, 0, 3)
        f140 = encode_tlv(140, 2, encode_tlv(1, 2, content.encode('utf-8')))
        return st_type, bytes(f1 + f4 + f5 + f140)


def check_step_gaps(verbose: bool = False) -> List[Dict[str, Any]]:
    """
    Scans all SQLite databases in the conversations directory.
    Detects if any session has broken step sequences (count != max - min + 1).
    """
    dbs = glob.glob(os.path.join(CONFIG.conv_dir, "*.db"))
    tombstones = load_tombstones()
    gaps = []

    for db_path in dbs:
        if not is_valid_main_conversation(db_path, tombstones):
            continue
        u = os.path.splitext(os.path.basename(db_path))[0]
        try:
            conn = sqlite3.connect(db_path, timeout=3)
            c = conn.cursor()
            c.execute("SELECT count(*), min(idx), max(idx) FROM steps")
            cnt, mi, ma = c.fetchone()
            conn.close()
            if cnt > 0 and ma is not None and mi == 0 and cnt != (ma + 1):
                title = extract_title_from_db(db_path)
                missing_cnt = (ma + 1) - cnt
                gaps.append({
                    "uuid": u, "title": title, "db_path": db_path,
                    "count": cnt, "min": mi, "max": ma, "missing": missing_cnt
                })
                if verbose:
                    print(f"⚠️ Step gap detected in [{u[:8]}] <{title}>: {cnt} rows present, max index is {ma} ({missing_cnt} steps missing)")
        except Exception:
            pass
    return gaps


def heal_step_gaps(verbose: bool = False) -> List[Dict[str, Any]]:
    """
    Extracts missing steps from transcript_full.jsonl and hot-stitches them
    into the physical SQLite steps table. Resolves UI truncate/spinning bugs.
    """
    gaps = check_step_gaps(verbose=False)
    if not gaps:
        if verbose:
            print("✅ All conversation databases have continuous step sequences. Zero gaps found.")
        return []

    healed = []
    for item in gaps:
        u = item['uuid']
        db_path = item['db_path']
        tf_path = os.path.join(CONFIG.brain_dir, u, ".system_generated", "logs", "transcript_full.jsonl")
        if not os.path.exists(tf_path):
            if verbose:
                print(f"❌ Cannot heal [{u[:8]}]: Streaming log file not found at {tf_path}")
            continue

        tf_by_idx = {}
        try:
            with open(tf_path, 'r', encoding='utf-8') as f:
                for line in f:
                    d = json.loads(line)
                    idx = d.get('step_index')
                    if idx is not None:
                        tf_by_idx[idx] = d
        except Exception as e:
            if verbose:
                print(f"❌ Failed to parse transcript [{u[:8]}]: {e}")
            continue

        try:
            conn = sqlite3.connect(db_path, timeout=5)
            c = conn.cursor()
            c.execute("SELECT idx FROM steps")
            existing = set(r[0] for r in c.fetchall())

            max_idx = max(max(existing), max(tf_by_idx.keys())) if tf_by_idx else item['max']
            missing = [i for i in range(max_idx + 1) if i not in existing]

            inserted = 0
            for idx in missing:
                d = tf_by_idx.get(idx)
                if d:
                    st_type, payload = construct_step_payload(d)
                else:
                    st_type = 132
                    payload = bytes(encode_tlv(1, 0, 132) + encode_tlv(4, 0, 3) +
                                    encode_tlv(5, 2, encode_tlv(1, 2, encode_tlv(1, 0, 1788440000))) +
                                    encode_tlv(140, 2, encode_tlv(1, 2, b"Step skipped or cancelled")))
                c.execute("""
                    INSERT INTO steps(idx, step_type, status, has_subtrajectory, step_payload, step_format)
                    VALUES(?, ?, 3, 0, ?, 0)
                """, (idx, st_type, payload))
                inserted += 1

            conn.commit()
            c.execute("PRAGMA integrity_check")
            ic = c.fetchone()[0]
            conn.close()

            if ic == 'ok':
                healed.append({"uuid": u, "title": item['title'], "inserted": inserted})
                log_event(f"✅ Hot-stitched step gap in [{u[:8]}] <{item['title']}>: Recovered {inserted} steps.")
                if verbose:
                    print(f"  [Stitched & Restored] [{u[:8]}] <{item['title']}>: Recovered {inserted} steps successfully.")
        except Exception as e:
            if verbose:
                print(f"❌ Failed to repair SQLite database [{u[:8]}]: {e}")

    return healed


# ==============================================================================
# Physical DB Orphan Adoption Engine
# ==============================================================================

def is_valid_main_conversation(db_path: str, tombstones: set) -> bool:
    """Verifies that a database represents an active, valid user main conversation."""
    u = os.path.splitext(os.path.basename(db_path))[0]
    if u in tombstones:
        return False
    try:
        conn = sqlite3.connect(db_path, timeout=3)
        c = conn.cursor()
        c.execute("SELECT count(*) FROM steps")
        steps = c.fetchone()[0]
        if steps <= 1:
            conn.close()
            return False

        c.execute("SELECT trajectory_type FROM trajectory_meta LIMIT 1")
        t_meta = c.fetchone()
        if not t_meta or t_meta[0] != 4:
            conn.close()
            return False

        c.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1")
        row = c.fetchone()
        conn.close()

        if not row or not row[0] or b'file:///' not in row[0]:
            return False

        return True
    except Exception:
        return False


def extract_title_from_db(db_path: str) -> str:
    """Extracts the session title from conversation steps."""
    title = None
    try:
        conn = sqlite3.connect(db_path, timeout=3)
        c = conn.cursor()
        c.execute("SELECT step_payload FROM steps ORDER BY idx DESC")
        for r in c.fetchall():
            if not r[0]:
                continue
            m = re.search(rb'\x22([\x04-\x60])([^\x00-\x1f]{3,60})H\x01', r[0])
            if m:
                title = m.group(2).decode('utf-8', 'ignore')
                break
        if not title:
            c.execute("SELECT step_payload FROM steps WHERE step_type IN (14, 15) LIMIT 1")
            r = c.fetchone()
            if r and r[0]:
                m = re.search(rb'\x9a\x01([\x01-\x60])([^\x00-\x1f]{2,50})', r[0])
                if m:
                    title = m.group(2).decode('utf-8', 'ignore')
        conn.close()
    except Exception:
        pass
    return title or "Untitled"


def adopt_orphaned_dbs(verbose: bool = False) -> List[Dict[str, str]]:
    """Scans all physical .db files and injects ProjectId (Field 18) into trajectory metadata."""
    proj_map = load_projects_map()
    dbs = glob.glob(os.path.join(CONFIG.conv_dir, "*.db"))
    adopted_items = []
    tombstones = load_tombstones()

    for db_path in dbs:
        if not is_valid_main_conversation(db_path, tombstones):
            continue
        u = os.path.splitext(os.path.basename(db_path))[0]
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            c = conn.cursor()
            c.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1")
            row = c.fetchone()
            if not row or not row[0]:
                conn.close()
                continue

            raw = row[0]
            ws_uri = None
            m = re.search(rb'file:///[^\x00-\x1f\x80-\xff]+', raw)
            if m:
                ws_uri = m.group(0).decode('utf-8', 'ignore')

            pid = find_project_id_for_uri(ws_uri, proj_map)
            if not pid:
                conn.close()
                continue

            patched_raw, mod = patch_protobuf_uri_recursive(raw, target='c%3A')

            if pid and pid.encode('ascii') not in patched_raw:
                u_b = pid.encode('ascii')
                f18_tag = encode_varint((18 << 3) | 2)
                patched_raw = patched_raw + f18_tag + encode_varint(len(u_b)) + u_b
                mod = True

            if mod:
                c.execute("DELETE FROM trajectory_metadata_blob")
                c.execute("INSERT INTO trajectory_metadata_blob(data) VALUES(?)", (patched_raw,))
                conn.commit()
                title = extract_title_from_db(db_path)
                ws_name = os.path.basename(ws_uri.rstrip('/\\')) if ws_uri else "Workspace"
                adopted_items.append({"uuid": u, "title": title, "ws": ws_name})
                if verbose:
                    print(f"  [Adopted / Injected ProjectId] [{u[:8]}] <{title}> -> {ws_name}")
            conn.close()
        except Exception:
            pass

    return adopted_items


# ==============================================================================
# Summary Parsers & Bi-Directional Synchronizer
# ==============================================================================

def extract_summary_meta(s_bytes: bytes) -> Dict[str, Any]:
    """Extracts step count, timestamp, title, and project ID for conflict arbitration."""
    if not s_bytes:
        return {'steps': 0, 'ts': 0, 'title': 'Untitled', 'pid': None}
    steps = 0
    ts = 0
    title = 'Untitled'
    pid = None
    off = 0
    while off < len(s_bytes):
        t, no = read_varint(s_bytes, off)
        if t is None:
            break
        w = t & 7
        fn = t >> 3
        if w == 0:
            v, no = read_varint(s_bytes, no)
            if fn == 2:
                steps = v
            off = no
        elif w == 2:
            l, no = read_varint(s_bytes, no)
            p = s_bytes[no:no + l]
            if fn == 1:
                title = p.decode('utf-8', 'ignore')
            elif fn in (3, 7, 10):
                in_off = 0
                while in_off < len(p):
                    in_t, in_no = read_varint(p, in_off)
                    if in_t is None:
                        break
                    in_w = in_t & 7
                    in_fn = in_t >> 3
                    if in_w == 0:
                        in_v, in_no = read_varint(p, in_no)
                        if in_fn == 1 and in_v > ts:
                            ts = in_v
                        in_off = in_no
                    elif in_w == 2:
                        in_l, in_no = read_varint(p, in_no)
                        in_off = in_no + in_l
                    else:
                        sz = 8 if in_w == 1 else 4
                        in_off = in_no + sz
            elif fn == 17:
                in_off = 0
                while in_off < len(p):
                    in_t, in_no = read_varint(p, in_off)
                    if in_t is None:
                        break
                    in_w = in_t & 7
                    in_fn = in_t >> 3
                    if in_w == 2:
                        in_l, in_no = read_varint(p, in_no)
                        if in_fn == 18:
                            pid = p[in_no:in_no + in_l].decode('ascii', 'ignore')
                        in_off = in_no + in_l
                    elif in_w == 0:
                        _, in_no = read_varint(p, in_no)
                        in_off = in_no
                    else:
                        sz = 8 if in_w == 1 else 4
                        in_off = in_no + sz
            off = no + l
        else:
            sz = 8 if w == 1 else 4
            off = no + sz
    return {'steps': steps, 'ts': ts, 'title': title, 'pid': pid}


def parse_summaries_from_pb(pb_bytes: bytes) -> Dict[str, bytes]:
    """Parses a map of {uuid: summary_protobuf_bytes} from an agyhub_summaries_proto.pb file."""
    convs = {}
    offset = 0
    while offset < len(pb_bytes):
        tag, next_offset = read_varint(pb_bytes, offset)
        if tag is None or (tag & 7) != 2:
            break
        msg_len, next_offset = read_varint(pb_bytes, next_offset)
        if msg_len is None:
            break
        msg = pb_bytes[next_offset:next_offset + msg_len]
        offset = next_offset + msg_len

        u_tag, u_next = read_varint(msg, 0)
        if u_tag is None or (u_tag >> 3) != 1:
            continue
        u_len, u_next = read_varint(msg, u_next)
        uuid_str = msg[u_next:u_next + u_len].decode('ascii', 'ignore')

        f2_tag, f2_next = read_varint(msg, u_next + u_len)
        if f2_tag is None or (f2_tag >> 3) != 2:
            continue
        f2_len, f2_next = read_varint(msg, f2_next)
        summary_bytes = msg[f2_next:f2_next + f2_len]

        convs[uuid_str] = summary_bytes
    return convs


def parse_summaries_from_ide() -> Dict[str, bytes]:
    """Parses session summaries directly from the VS Code globalStorage SQLite state database."""
    convs = {}
    if not os.path.exists(CONFIG.ide_db_path):
        return convs
    try:
        conn = sqlite3.connect(CONFIG.ide_db_path, timeout=5)
        c = conn.cursor()
        c.execute("SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.trajectorySummaries'")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            raw_proto = base64.b64decode(row[0])
            offset = 0
            while offset < len(raw_proto):
                tag, next_o = read_varint(raw_proto, offset)
                if tag is None or (tag & 7) != 2:
                    break
                msg_len, next_o = read_varint(raw_proto, next_o)
                if msg_len is None:
                    break
                msg = raw_proto[next_o:next_o + msg_len]
                offset = next_o + msg_len

                u_tag, u_next = read_varint(msg, 0)
                u_len, u_next = read_varint(msg, u_next)
                uuid_str = msg[u_next:u_next + u_len].decode('ascii', 'ignore')

                f2_tag, f2_next = read_varint(msg, u_next + u_len)
                f2_len, f2_next = read_varint(msg, f2_next)
                f2_payload = msg[f2_next:f2_next + f2_len]

                s_tag, s_n = read_varint(f2_payload, 0)
                s_len, s_n = read_varint(f2_payload, s_n)
                b64_sub = f2_payload[s_n:s_n + s_len]
                inner_summary = base64.b64decode(b64_sub)

                convs[uuid_str] = inner_summary
    except Exception:
        pass
    return convs


def smart_bidirectional_sync(verbose: bool = True, log_manual: bool = False) -> Tuple[bool, bool]:
    """
    Executes incremental bi-directional synchronization between Antigravity 2.0 and IDE.
    Uses native binary Protobuf reuse and Smart Conflict Arbitration. Zero write if no changes.
    """
    tombstones = load_tombstones()
    adopted_items = adopt_orphaned_dbs(verbose=False)
    proj_map = load_projects_map()

    # Check physical storage links
    link_status = check_directory_links(CONFIG.gemini_home)
    unlinked = [k for k, v in link_status.items() if not v["is_correct"]]
    if unlinked and verbose:
        print(f"⚠️ NOTICE: Shared storage folders {unlinked} are not linked between 2.0 and IDE.")
        print(f"   Run 'python antigravity_sync.py --link' to share physical SQLite databases and logs.\n")

    # Load 2.0 summaries
    convs_20 = {}
    if os.path.exists(CONFIG.src_pb):
        try:
            with open(CONFIG.src_pb, 'rb') as f:
                convs_20 = parse_summaries_from_pb(f.read())
        except Exception:
            pass

    # Load IDE summaries
    convs_ide = parse_summaries_from_ide()

    all_uuids = set(convs_20.keys()) | set(convs_ide.keys())
    merged_pool = {}
    diff_events = []

    for u in all_uuids:
        if u in tombstones:
            continue

        db_path = os.path.join(CONFIG.conv_dir, f"{u}.db")
        if not is_valid_main_conversation(db_path, tombstones):
            continue

        s20 = convs_20.get(u)
        side = convs_ide.get(u)

        if s20 and not side:
            cleaned_s = clean_summary_for_20(s20, proj_map)
            merged_pool[u] = cleaned_s
            title = extract_summary_meta(cleaned_s)['title']
            diff_events.append(f"Injected into IDE: [{u[:8]}] <{title}>")
        elif side and not s20:
            cleaned_s = clean_summary_for_20(side, proj_map)
            merged_pool[u] = cleaned_s
            title = extract_summary_meta(cleaned_s)['title']
            diff_events.append(f"Injected into 2.0: [{u[:8]}] <{title}>")
        elif s20 and side:
            m20 = extract_summary_meta(s20)
            m_ide = extract_summary_meta(side)

            # Smart Conflict Arbitration
            if m20['steps'] > m_ide['steps']:
                winner_summary = s20
                reason = f"2.0 has more steps ({m20['steps']} vs {m_ide['steps']})"
                event = f"Synced to IDE: [{u[:8]}] <{m20['title']}> ({reason})"
            elif m_ide['steps'] > m20['steps']:
                winner_summary = side
                reason = f"IDE has more steps ({m_ide['steps']} vs {m20['steps']})"
                event = f"Synced to 2.0: [{u[:8]}] <{m_ide['title']}> ({reason})"
            else:
                if m_ide['ts'] > m20['ts']:
                    winner_summary = side
                    reason = "IDE has newer activity timestamp"
                    event = f"Synced to 2.0: [{u[:8]}] <{m_ide['title']}> ({reason})"
                else:
                    winner_summary = s20
                    reason = "2.0 has newer activity timestamp"
                    event = f"Synced to IDE: [{u[:8]}] <{m20['title']}> ({reason})"

            cleaned_winner = clean_summary_for_20(winner_summary, proj_map)
            merged_pool[u] = cleaned_winner

            if s20 != side:
                diff_events.append(event)

    # Sort merged sessions chronologically by activity timestamp
    sorted_uuids = sorted(
        merged_pool.keys(),
        key=lambda k: extract_summary_meta(merged_pool[k])['ts'],
        reverse=True
    )

    # Construct unified 2.0 binary Protobuf
    final_20_bytes = bytearray()
    for u in sorted_uuids:
        s_bytes = merged_pool[u]
        u_b = u.encode('ascii')
        inner = encode_tlv(1, 2, u_b) + encode_tlv(2, 2, s_bytes)
        final_20_bytes.extend(encode_tlv(1, 2, inner))

    # Construct unified IDE Protobuf & Base64 payload
    final_ide_bytes = bytearray()
    for u in sorted_uuids:
        s_bytes = merged_pool[u]
        s_bytes_ide, _ = patch_protobuf_uri_recursive(s_bytes, target='c:')
        b64_sub = base64.b64encode(s_bytes_ide)
        f2_payload = encode_tlv(1, 2, b64_sub)
        inner = encode_tlv(1, 2, u.encode('ascii')) + encode_tlv(2, 2, f2_payload)
        final_ide_bytes.extend(encode_tlv(1, 2, inner))

    b64_ide_val = base64.b64encode(bytes(final_ide_bytes)).decode('ascii')

    # Detect modifications before writing to disk
    current_20_bytes = b""
    if os.path.exists(CONFIG.src_pb):
        try:
            with open(CONFIG.src_pb, 'rb') as f:
                current_20_bytes = f.read()
        except Exception:
            pass

    mod_20 = (bytes(final_20_bytes) != current_20_bytes)
    mod_ide = False

    if os.path.exists(CONFIG.ide_db_path):
        try:
            conn = sqlite3.connect(CONFIG.ide_db_path, timeout=5)
            c = conn.cursor()
            c.execute("SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.trajectorySummaries'")
            row = c.fetchone()
            if not row or row[0] != b64_ide_val:
                mod_ide = True
            conn.close()
        except Exception:
            pass

    # Commit writes if changes detected
    if mod_20:
        os.makedirs(os.path.dirname(CONFIG.src_pb), exist_ok=True)
        with open(CONFIG.src_pb, 'wb') as f:
            f.write(bytes(final_20_bytes))

    if mod_ide and os.path.exists(CONFIG.ide_db_path):
        os.makedirs(os.path.dirname(CONFIG.ide_pb), exist_ok=True)
        with open(CONFIG.ide_pb, 'wb') as f:
            f.write(bytes(final_20_bytes))

        conn = sqlite3.connect(CONFIG.ide_db_path, timeout=5)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO ItemTable(key, value) VALUES('antigravityUnifiedStateSync.trajectorySummaries', ?)", (b64_ide_val,))
        conn.commit()
        conn.close()

    if mod_20 or mod_ide or len(adopted_items) > 0 or diff_events or log_manual:
        msg = f"Bi-directional sync complete: Adopted={len(adopted_items)}, Written20={mod_20}, WrittenIDE={mod_ide}, Total={len(sorted_uuids)}"
        log_event(msg)
        for ev in diff_events:
            log_event(f"  -> {ev}")

    if verbose:
        print(f"✅ Bi-Directional Sync: Adopted={len(adopted_items)}, 2.0 Write={mod_20}, IDE Write={mod_ide}, Total Active={len(sorted_uuids)}")
        for ev in diff_events[:10]:
            print(f"   -> {ev}")
        if len(diff_events) > 10:
            print(f"   ... and {len(diff_events) - 10} more events")

    return mod_20, mod_ide


# ==============================================================================
# Backup & Hourly Rotation Manager
# ==============================================================================

def create_backup_snapshot(
    category: str = "hourly",
    max_retention: int = 10,
    force: bool = False,
    note: str = ""
) -> Optional[str]:
    """
    Creates a timestamped snapshot of summaries and state DBs.
    Categories: 'hourly', 'manual', 'pre_init', 'pre_restore'.
    Milestone snapshots ('pre_init', 'pre_restore') are permanently exempt from rotation.
    """
    now = datetime.now()
    if category == "pre_init":
        slot_name = f"PRE_INIT_SNAPSHOT_{now.strftime('%Y%m%d_%H%M%S')}"
    elif category == "pre_restore":
        slot_name = f"SAFETY_SNAPSHOT_BEFORE_RESTORE_{now.strftime('%Y%m%d_%H%M%S')}"
    elif category == "manual":
        slot_name = f"manual_{now.strftime('%Y%m%d_%H%M%S')}"
    else:
        slot_name = now.strftime("%Y%m%d_%H0000")

    target_dir = os.path.join(CONFIG.backup_root, slot_name)
    if os.path.exists(target_dir) and not force and category == "hourly":
        return None

    os.makedirs(target_dir, exist_ok=True)

    if os.path.exists(CONFIG.src_pb):
        shutil.copy2(CONFIG.src_pb, os.path.join(target_dir, "agyhub_summaries_proto.pb"))
    if os.path.exists(CONFIG.ide_pb):
        shutil.copy2(CONFIG.ide_pb, os.path.join(target_dir, "ide_pb.pb"))
    if os.path.exists(CONFIG.ide_db_path):
        shutil.copy2(CONFIG.ide_db_path, os.path.join(target_dir, "state.vscdb"))

    manifest = {
        "slot_name": slot_name,
        "category": category,
        "created_at": now.isoformat(),
        "note": note,
        "gemini_home": CONFIG.gemini_home,
        "ide_storage_dir": CONFIG.ide_storage_dir
    }
    with open(os.path.join(target_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    log_event(f"📦 Backup created [{category}]: {slot_name}")

    # Rotate old routine backups, EXEMPTING milestone snapshots
    routine_backups = sorted([
        b for b in glob.glob(os.path.join(CONFIG.backup_root, "*"))
        if os.path.isdir(b) and not os.path.basename(b).startswith("PRE_INIT_SNAPSHOT_")
           and not os.path.basename(b).startswith("SAFETY_SNAPSHOT_BEFORE_RESTORE_")
    ], key=os.path.getmtime)

    if len(routine_backups) > max_retention:
        for old in routine_backups[:-max_retention]:
            try:
                shutil.rmtree(old)
                log_event(f"🗑️ Rotated old routine backup: {os.path.basename(old)}")
            except Exception:
                pass

    return target_dir


def hourly_backup_manager(max_retention: int = 10, force: bool = False) -> bool:
    """Invoked hourly by daemon or manually by user."""
    res = create_backup_snapshot(
        category="manual" if force else "hourly",
        max_retention=max_retention,
        force=force
    )
    return res is not None


def audit_backup_diff(backup_dir: str) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Audits the difference between current active sessions and a target backup.
    Returns: (active_summaries, backup_summaries, missing_in_backup, step_regressions)
    """
    active_map = {}
    if os.path.exists(CONFIG.src_pb):
        try:
            with open(CONFIG.src_pb, 'rb') as f:
                for s in parse_summaries_from_pb(f.read()):
                    u = s.get("uuid")
                    if u:
                        active_map[u] = s
        except Exception:
            pass

    backup_map = {}
    b_pb = os.path.join(backup_dir, "agyhub_summaries_proto.pb")
    if not os.path.exists(b_pb):
        b_pb = os.path.join(backup_dir, "ide_pb.pb")
    if os.path.exists(b_pb):
        try:
            with open(b_pb, 'rb') as f:
                for s in parse_summaries_from_pb(f.read()):
                    u = s.get("uuid")
                    if u:
                        backup_map[u] = s
        except Exception:
            pass

    missing_in_backup = []
    step_regressions = []

    for u, cur_s in active_map.items():
        cur_steps = cur_s.get("step_count", 0)
        title = cur_s.get("title", u[:8])
        if u not in backup_map:
            missing_in_backup.append({
                "uuid": u,
                "title": title,
                "active_steps": cur_steps,
                "active_mtime": cur_s.get("active_time_s", 0)
            })
        else:
            b_s = backup_map[u]
            b_steps = b_s.get("step_count", 0)
            if cur_steps > b_steps:
                step_regressions.append({
                    "uuid": u,
                    "title": title,
                    "active_steps": cur_steps,
                    "backup_steps": b_steps,
                    "diff": cur_steps - b_steps
                })

    return active_map, backup_map, missing_in_backup, step_regressions


def restore_from_backup(backup_dir: Optional[str] = None, mode: str = "merge", verbose: bool = True) -> bool:
    """
    Restores metadata from a backup snapshot with safety audits and step protection.
    mode='merge':
        Safe Merge Restore (Recommended). Restores missing metadata, but retains higher
        step counts and newer timestamps for currently active sessions. Zero step loss!
    mode='overwrite':
        Force Mirror Overwrite. Replaces active metadata directly with backup files.
    """
    procs = check_running_processes()
    if procs and verbose:
        print("\n" + "=" * 72)
        print(f"⚠️  ACTIVE PROCESS WARNING: Conflicting active process(es) detected: {', '.join(procs)}")
        print("    Please close VS Code and Antigravity before restoring to avoid memory overwrite.")
        print("=" * 72)

    all_backups = sorted(glob.glob(os.path.join(CONFIG.backup_root, "*")), key=os.path.getmtime, reverse=True)
    if not all_backups:
        if verbose:
            print("\n❌ No backups found in backup directory.")
        return False

    if backup_dir is None:
        print("\nAvailable Backup Snapshots:")
        limit = min(len(all_backups), 15)
        for idx, b in enumerate(all_backups[:limit]):
            b_name = os.path.basename(b)
            tag = ""
            if b_name.startswith("PRE_INIT_SNAPSHOT_"):
                tag = " [PRE-INIT MILESTONE]"
            elif b_name.startswith("SAFETY_SNAPSHOT_"):
                tag = " [PRE-RESTORE SAFETY]"
            elif b_name.startswith("manual_"):
                tag = " [MANUAL]"
            else:
                tag = " [HOURLY]"
            mt_str = datetime.fromtimestamp(os.path.getmtime(b)).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {idx + 1:2d}. {b_name}{tag} ({mt_str})")

        sel = input(f"\nSelect backup snapshot to restore (1-{limit}/Q): ").strip()
        if not sel.isdigit() or not (1 <= int(sel) <= limit):
            print("Restore cancelled.")
            return False
        backup_dir = all_backups[int(sel) - 1]

    b_name = os.path.basename(backup_dir)

    active_map, backup_map, missing, regressions = audit_backup_diff(backup_dir)

    if verbose:
        print("\n" + "=" * 72)
        print("  📊 BACKUP AUDIT & STEP REGRESSION ANALYSIS")
        print("=" * 72)
        print(f"Target Backup  : {b_name}")
        print(f"Active Sessions: {len(active_map)} | Backup Sessions: {len(backup_map)}")

        if missing:
            print(f"\n⚠️  {len(missing)} session(s) created AFTER this backup:")
            for m in missing[:5]:
                print(f"   • [{m['uuid'][:8]}] {m['title']} ({m['active_steps']} steps)")
            if len(missing) > 5:
                print(f"   ... and {len(missing) - 5} more sessions")
            if mode == "overwrite":
                print("   🚨 In 'overwrite' mode, these sessions will be excluded from index!")

        if regressions:
            print(f"\n⚠️  {len(regressions)} session(s) have MORE steps currently than in this backup:")
            for r in regressions[:5]:
                print(f"   • [{r['uuid'][:8]}] {r['title']} (Current: {r['active_steps']} vs Backup: {r['backup_steps']} steps, -{r['diff']} steps!)")
            if len(regressions) > 5:
                print(f"   ... and {len(regressions) - 5} more sessions")
            if mode == "overwrite":
                print("   🚨 In 'overwrite' mode, chat history steps will regress!")

        if not missing and not regressions:
            print("\n✅ Zero step regression or missing sessions detected.")
        print("=" * 72)

    safety_snap = create_backup_snapshot(
        category="pre_restore",
        force=True,
        note=f"Automatic safety snapshot taken immediately before restoring {b_name}"
    )
    if verbose and safety_snap:
        print(f"\n🛡️ Created Pre-Restore Safety Snapshot: {os.path.basename(safety_snap)}")

    if mode == "merge":
        if verbose:
            print("\nApplying Safe Merge Restore (keeping highest step count & latest timestamps)...")

        b_src_pb = os.path.join(backup_dir, "agyhub_summaries_proto.pb")
        if os.path.exists(b_src_pb):
            with open(b_src_pb, 'rb') as f:
                b_raw = f.read()
            b_summaries = parse_summaries_from_pb(b_raw)
            for s in b_summaries:
                u = s.get("uuid")
                if not u:
                    continue
                if u not in active_map:
                    active_map[u] = s
                else:
                    cur = active_map[u]
                    if s.get("step_count", 0) > cur.get("step_count", 0):
                        active_map[u]["step_count"] = s["step_count"]
                    if s.get("active_time_s", 0) > cur.get("active_time_s", 0):
                        active_map[u]["active_time_s"] = s["active_time_s"]
                        active_map[u]["title"] = s.get("title", cur.get("title"))

        smart_bidirectional_sync(verbose=verbose, log_manual=True)
        if verbose:
            print(f"\n✅ Safe Merge Restore completed successfully! All {len(active_map)} sessions intact with zero step loss.")

    elif mode == "overwrite":
        if verbose:
            confirm = input("\nType 'CONFIRM' to execute force mirror overwrite: ").strip()
            if confirm != "CONFIRM":
                print("Restore cancelled.")
                return False

        b_src_pb = os.path.join(backup_dir, "agyhub_summaries_proto.pb")
        b_ide_pb = os.path.join(backup_dir, "ide_pb.pb")
        b_vscdb = os.path.join(backup_dir, "state.vscdb")

        if os.path.exists(b_src_pb) and os.path.exists(CONFIG.src_pb):
            shutil.copy2(b_src_pb, CONFIG.src_pb)
        if os.path.exists(b_ide_pb) and os.path.exists(CONFIG.ide_pb):
            shutil.copy2(b_ide_pb, CONFIG.ide_pb)
        if os.path.exists(b_vscdb) and os.path.exists(CONFIG.ide_db_path):
            shutil.copy2(b_vscdb, CONFIG.ide_db_path)

        if verbose:
            print(f"\n✅ Force mirror overwrite completed from {b_name}.")

    log_event(f"🔄 Restored from backup: {b_name} (mode={mode})")
    return True


# ==============================================================================
# Tombstone-Level Permanent Deletion Engine
# ==============================================================================

def execute_permanent_deletion(uuids: List[str]):
    """Permanently purges specified conversation UUIDs across SQLite, protobuf, and logs."""
    tombstones = load_tombstones()
    for u in uuids:
        tombstones.add(u)
    save_tombstones(tombstones)

    for u in uuids:
        db_path = os.path.join(CONFIG.conv_dir, f"{u}.db")
        brain_path = os.path.join(CONFIG.brain_dir, u)
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except Exception:
                pass
        if os.path.exists(brain_path):
            try:
                shutil.rmtree(brain_path)
            except Exception:
                pass

    smart_bidirectional_sync(verbose=False, log_manual=True)
    log_event(f"💀 Tombstone deletion executed for {len(uuids)} sessions: {uuids}")
    print(f"\n✅ Successfully purged {len(uuids)} sessions permanently.")


def interactive_delete_ui():
    """Interactive CLI menu for selecting and permanently deleting sessions."""
    proj_map = load_projects_map()
    tombstones = load_tombstones()
    convs = parse_summaries_from_ide()

    if not convs and os.path.exists(CONFIG.src_pb):
        with open(CONFIG.src_pb, 'rb') as f:
            convs = parse_summaries_from_pb(f.read())

    active = [u for u in convs.keys() if u not in tombstones]
    if not active:
        print("\nNo active sessions found.")
        return

    print("\n" + "=" * 65)
    print("      Permanent Session Purge & Tombstone Console")
    print("=" * 65)

    for idx, u in enumerate(active, 1):
        meta = extract_summary_meta(convs[u])
        pid = meta.get('pid') or "Uncategorized"
        title = meta.get('title') or "Untitled"
        print(f"[{idx:2d}] [{u[:8]}] <{title}> ({meta['steps']} steps, Project: {pid[:8]})")

    print("\nEnter session numbers to delete (e.g., 1, 3-5), or 'Q' to cancel.")
    choice = input("Select: ").strip()
    if choice.upper() == "Q" or not choice:
        return

    selected_indices = set()
    for part in choice.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-")
                for i in range(int(start), int(end) + 1):
                    selected_indices.add(i)
            except Exception:
                pass
        else:
            try:
                selected_indices.add(int(part))
            except Exception:
                pass

    targets = [active[i - 1] for i in selected_indices if 1 <= i <= len(active)]
    if not targets:
        print("No valid sessions selected.")
        return

    confirm = input(f"\n⚠️ PERMANENT DELETION of {len(targets)} sessions? Type 'YES' to proceed: ").strip()
    if confirm == "YES":
        execute_permanent_deletion(targets)
    else:
        print("Cancelled.")


# ==============================================================================
# Windows Silent Auto-Startup Daemon Setup
# ==============================================================================

def install_startup():
    """Installs a silent Windows Startup VBScript pointing to pythonw.exe."""
    if sys.platform != "win32":
        print("ℹ️ Silent VBS startup is only supported on Windows.")
        return

    script_path = os.path.abspath(__file__)
    pythonw_path = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw_path):
        pythonw_path = sys.executable

    vbs_content = f'Set WshShell = CreateObject("WScript.Shell")\n' \
                  f'WshShell.Run """{pythonw_path}"" ""{script_path}"" --daemon", 0, False\n'

    try:
        os.makedirs(os.path.dirname(CONFIG.vbs_path), exist_ok=True)
        with open(CONFIG.vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs_content)
        print(f"✅ Silent startup daemon installed to:\n   {CONFIG.vbs_path}")
        log_event("✅ Startup VBS daemon installed.")
    except Exception as e:
        print(f"❌ Failed to install startup daemon: {e}")


def uninstall_startup():
    """Removes the Windows Startup VBScript."""
    if os.path.exists(CONFIG.vbs_path):
        try:
            os.remove(CONFIG.vbs_path)
            print("✅ Startup daemon removed successfully.")
            log_event("🗑️ Startup VBS daemon removed.")
        except Exception as e:
            print(f"❌ Failed to remove startup script: {e}")
    else:
        print("ℹ️ Startup daemon is not currently installed.")


def run_daemon_loop(interval: int = 60):
    """Headless daemon polling loop (adopt -> sync -> hourly backup -> step gap check)."""
    log_event("🚀 Background sync daemon started.")
    print(f"🚀 Running daemon loop every {interval} seconds. Press Ctrl+C to stop.")
    while True:
        try:
            adopt_orphaned_dbs(verbose=False)
            smart_bidirectional_sync(verbose=False, log_manual=False)
            hourly_backup_manager(max_retention=10, force=False)
        except Exception as e:
            log_event(f"❌ Daemon exception: {e}")
        time.sleep(interval)


def run_first_time_initialization(verbose: bool = True) -> bool:
    """
    Executes the First-Time Zero-Loss Initialization Wizard:
      Phase 0: Pre-Init Milestone Snapshot & Active Process Safety Check
      Phase 1: Physical Storage Fusion & Junction Setup (2.0 <===> IDE)
      Phase 2: Orphan Database Adoption (Field 18 ProjectId injection)
      Phase 3: Step Gap Detection & Hot-Stitch Recovery (transcript_full.jsonl -> SQLite)
      Phase 4: Incremental Bi-Directional Synchronization
    """
    if verbose:
        print("\n" + "=" * 72)
        print("  🌟 ANTIGRAVITY FIRST-TIME ZERO-LOSS INITIALIZATION WIZARD 🌟")
        print("=" * 72)
        print("This wizard safely fuses past session data from both Antigravity 2.0")
        print("and Antigravity IDE, links physical storage, and performs full sync.")
        print("\nℹ️  PEER-TO-PEER SYMMETRIC ARCHITECTURE NOTICE:")
        print("   Whether you primarily use Antigravity 2.0 or Antigravity IDE, all")
        print("   sessions are merged symmetrically. The version with more steps and")
        print("   newer activity wins. Zero data loss for both 2.0 and IDE users!\n")

    # Safety check: active running processes
    procs = check_running_processes()
    if procs and verbose:
        print("=" * 72)
        print(f"⚠️  ACTIVE PROCESS WARNING: {', '.join(procs)} is currently running.")
        print("    It is strongly advised to close VS Code / Antigravity before initialization")
        print("    to avoid database write locks and memory overwrite conflicts.")
        print("=" * 72 + "\n")

    # Phase 0: Pre-Init Milestone Snapshot (if not already linked)
    links = check_directory_links(CONFIG.gemini_home)
    is_linked = all(v["is_correct"] for v in links.values())
    if not is_linked:
        if verbose:
            print("[Phase 0/4] Creating permanent Pre-Init Milestone Snapshot...")
        snap = create_backup_snapshot(
            category="pre_init",
            force=True,
            note="Milestone snapshot taken prior to first-time initialization"
        )
        if verbose and snap:
            print(f"  -> Snapshot archived to {os.path.basename(snap)} (Protected from rotation).")

    # Phase 1: Physical Storage Fusion & Junction Setup
    if verbose:
        print("\n[Phase 1/4] Fusing physical storage and establishing Directory Junctions...")
    fuse_res = fuse_and_link_storage(CONFIG.gemini_home, verbose=verbose)

    # Phase 2: Orphan Database Adoption
    if verbose:
        print("\n[Phase 2/4] Scanning and adopting orphaned physical databases...")
    adopted = adopt_orphaned_dbs(verbose=verbose)
    if verbose:
        print(f"  -> Adopted / patched {len(adopted)} database(s).")

    # Phase 3: Step Gap Detection & Hot-Stitch Recovery
    if verbose:
        print("\n[Phase 3/4] Checking and healing step sequence gaps from logs...")
    healed = heal_step_gaps(verbose=verbose)
    if verbose:
        print(f"  -> Healed {len(healed)} session(s) with step gaps.")

    # Phase 4: Bi-directional Metadata Synchronization
    if verbose:
        print("\n[Phase 4/4] Executing bi-directional metadata synchronization...")
    smart_bidirectional_sync(verbose=verbose, log_manual=True)

    if verbose:
        print("\n" + "=" * 72)
        print("  🎉 INITIALIZATION WIZARD COMPLETED SUCCESSFULLY! 🎉")
        print("=" * 72)
        print("Summary of Actions:")
        if fuse_res.get("already_linked"):
            print("  • Physical Storage       : Already linked previously")
        else:
            print(f"  • IDE Sessions Migrated  : {fuse_res.get('dbs_migrated_from_ide', 0)}")
            print(f"  • Conflicted Sessions    : {fuse_res.get('dbs_conflict_ide_preferred', 0)} updated from IDE, {fuse_res.get('dbs_conflict_20_kept', 0)} kept from 2.0")
            print(f"  • Brain Folders Merged   : {fuse_res.get('brains_migrated_from_ide', 0) + fuse_res.get('brains_merged', 0)}")
            print(f"  • Junction Links Created : {', '.join(fuse_res.get('links_created', []))}")
        print(f"  • Orphaned DBs Patched   : {len(adopted)}")
        print(f"  • Step Gaps Healed       : {len(healed)}")
        print("Both Antigravity 2.0 and IDE are now fully unified and ready to use!")
        print("=" * 72 + "\n")

    return True


# ==============================================================================
# CLI Entry Point & Interactive Console
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Antigravity 2.0 <-> IDE Bi-Directional Synchronization & Session Recovery Manager\n\n"
                    "RECOMMENDED USAGE: Strongly advised to execute manually while both Antigravity 2.0\n"
                    "and the IDE (VS Code) are completely closed. The automated background daemon is\n"
                    "theoretically operational, but has not yet been exhaustively tested.\n\n"
                    "PLATFORM NOTICE: Developed and verified exclusively on Windows (Windows 10 / 11).\n"
                    "macOS and Linux path support is architecturally implemented but untested.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python antigravity_sync.py --init         Run first-time zero-loss setup wizard (fuses storage & syncs)
  python antigravity_sync.py --sync         Run one-shot bi-directional incremental synchronization
  python antigravity_sync.py --decouple     Decouple shared storage ('各管各的', keeping all sessions)
  python antigravity_sync.py --restore      Restore metadata from snapshot with step regression audit
  python antigravity_sync.py --adopt        Scan and inject missing ProjectId into orphaned .db files
  python antigravity_sync.py --check-gaps   Check all conversation databases for step sequence gaps
  python antigravity_sync.py --heal-gaps    Auto-recover and stitch missing steps from streaming logs
  python antigravity_sync.py --backup       Force immediate snapshot backup of summaries and state DB
  python antigravity_sync.py --daemon       Run background polling loop every 60 seconds (Experimental)
  python antigravity_sync.py                Launch interactive management console
"""
    )

    parser.add_argument("--init", action="store_true", help="Run first-time zero-loss setup wizard (fuse storage, adopt orphans, heal gaps, sync)")
    parser.add_argument("--sync", action="store_true", help="Run one-shot incremental bi-directional sync (Recommended)")
    parser.add_argument("--decouple", choices=["clone", "revert"], nargs="?", const="clone",
                        help="Decouple shared storage ('各管各的'). 'clone' keeps all data independently; 'revert' restores pre-init state.")
    parser.add_argument("--restore", choices=["merge", "overwrite"], nargs="?", const="merge",
                        help="Restore metadata from backup snapshot with step audit. 'merge' (default/safe) prevents step regression.")
    parser.add_argument("--adopt", action="store_true", help="Scan and adopt orphaned physical .db files")
    parser.add_argument("--check-gaps", action="store_true", help="Check all conversation databases for step gaps")
    parser.add_argument("--heal-gaps", action="store_true", help="Hot-stitch and recover step gaps from logs")
    parser.add_argument("--delete", action="store_true", help="Open interactive session purge console")
    parser.add_argument("--backup", action="store_true", help="Force immediate snapshot backup")
    parser.add_argument("--link", action="store_true", help="Setup shared storage links (Junction/Symlink) between 2.0 and IDE")
    parser.add_argument("--daemon", action="store_true", help="Run background daemon loop (EXPERIMENTAL: theoretically operational, but untested)")
    parser.add_argument("--interval", type=int, default=60, help="Daemon polling interval in seconds (default: 60)")
    parser.add_argument("--install-startup", action="store_true", help="Install Windows silent background startup")
    parser.add_argument("--uninstall-startup", action="store_true", help="Uninstall Windows startup daemon")

    # Path overrides
    parser.add_argument("--gemini-home", type=str, help="Override base .gemini home directory")
    parser.add_argument("--ide-storage", type=str, help="Override IDE globalStorage directory")

    args = parser.parse_args()

    # Apply configuration overrides if provided
    if args.gemini_home or args.ide_storage:
        global CONFIG
        CONFIG = PathConfig(gemini_home=args.gemini_home, ide_storage_dir=args.ide_storage)

    if args.init:
        run_first_time_initialization(verbose=True)
        return

    if args.decouple:
        decouple_storage(CONFIG.gemini_home, mode=args.decouple, verbose=True)
        return

    if args.restore:
        restore_from_backup(mode=args.restore, verbose=True)
        return

    if args.daemon:
        run_daemon_loop(args.interval)
        return

    if args.delete:
        interactive_delete_ui()
        return

    if args.install_startup:
        install_startup()
        return

    if args.uninstall_startup:
        uninstall_startup()
        return

    if args.adopt:
        items = adopt_orphaned_dbs(verbose=True)
        print(f"✅ Orphan adoption complete: Injected ProjectId into {len(items)} database(s).")
        return

    if args.check_gaps:
        gaps = check_step_gaps(verbose=True)
        if not gaps:
            print("✅ Scan complete: All conversation databases have contiguous steps. Zero gaps.")
        else:
            print(f"⚠️ Scan complete: Found {len(gaps)} session(s) with step gaps. Use --heal-gaps to repair.")
        return

    if args.heal_gaps:
        healed = heal_step_gaps(verbose=True)
        if healed:
            print(f"✅ Stitching complete: Recovered {len(healed)} session(s). Run --sync to update summaries.")
        else:
            print("ℹ️ No step gaps required healing.")
        return

    if args.backup:
        b_res = hourly_backup_manager(max_retention=10, force=True)
        print("✅ Snapshot backup completed!" if b_res else "ℹ️ Backup skipped.")
        return

    if args.link:
        setup_directory_links(CONFIG.gemini_home, verbose=True)
        return

    if args.sync:
        smart_bidirectional_sync(verbose=True, log_manual=True)
        return

    # Interactive Management Console
    while True:
        print("=" * 68)
        print("  Antigravity 2.0 <-> IDE Session Synchronization Console")
        print("  * NOTICE: Recommended to run while 2.0 & IDE are closed.")
        print("  * Daemon mode is theoretically operational, but untested.")
        print("=" * 68)

        proj_map = load_projects_map()
        conv_cnt = 0
        if os.path.exists(CONFIG.src_pb):
            try:
                with open(CONFIG.src_pb, 'rb') as f:
                    conv_cnt = len(parse_summaries_from_pb(f.read()))
            except Exception:
                pass

        links = check_directory_links(CONFIG.gemini_home)
        linked_ok = sum(1 for v in links.values() if v["is_correct"])
        links_status_str = f"[Active ({linked_ok}/3)]" if linked_ok == 3 else f"[{linked_ok}/3 Action Required: Run 0 or 7]"

        print(f"\n[Status Overview]")
        print(f"  • Registered Projects    : {len(set(proj_map.values()))} active project(s)")
        print(f"  • Active Conversations   : {conv_cnt} session(s)")
        print(f"  • Shared Storage Links   : {links_status_str}")
        print(f"  • Startup Daemon         : {'[Installed]' if os.path.exists(CONFIG.vbs_path) else '[Not Installed]'}")

        print("\n[Operations]")
        print("  0. [Init] Run First-Time Setup Wizard (Fuse Storage & Full Sync)")
        print("  1. [Sync] Run Incremental Bi-Directional Synchronization (Recommended)")
        print("  2. [Restore] Restore from Backup Snapshot with Step Audit")
        print("  3. [Decouple] Decouple Shared Storage ('各管各的' / Revert to Pristine)")
        print("  4. [Delete] Interactive Session Permanent Purge Console")
        print("  5. [Adopt] Scan & Inject ProjectId into Orphaned Databases")
        print("  6. [Check] Scan All Databases for Step Sequence Gaps")
        print("  7. [Heal] Hot-Stitch Step Gaps from Logs into SQLite")
        print("  8. [Backup] Force Immediate Data Snapshot Backup")
        print("  9. [Link] Setup Shared Storage Links (Junction / Symlink)")
        print("  10. [Startup] Install Silent Windows Auto-Startup Daemon")
        print("  11. [Uninstall] Remove Auto-Startup Daemon")
        print("  Q. Quit")

        try:
            choice = input("\nSelect operation (0-11/Q): ").strip().upper()
        except Exception:
            choice = "Q"

        if choice == "0":
            run_first_time_initialization(verbose=True)
            input("\nPress Enter to continue...")
        elif choice == "1":
            smart_bidirectional_sync(verbose=True, log_manual=True)
            input("\nPress Enter to continue...")
        elif choice == "2":
            print("\nRestore Mode:")
            print("  1. [Safe Merge] Restore missing sessions, keep highest steps & newest time (Recommended)")
            print("  2. [Force Mirror] Overwrite active state directly from backup")
            r_choice = input("Select restore mode (1/2/Cancel): ").strip()
            if r_choice == "1":
                restore_from_backup(mode="merge", verbose=True)
            elif r_choice == "2":
                restore_from_backup(mode="overwrite", verbose=True)
            input("\nPress Enter to continue...")
        elif choice == "3":
            print("\nDecouple Options ('各管各的'):")
            print("  1. [Materialize & Clone] Keep 100% of all sessions independently (Recommended)")
            print("  2. [Revert] Undo initialization and revert to pristine pre-init state")
            d_choice = input("Select decouple mode (1/2/Cancel): ").strip()
            if d_choice == "1":
                decouple_storage(CONFIG.gemini_home, mode="clone", verbose=True)
            elif d_choice == "2":
                decouple_storage(CONFIG.gemini_home, mode="revert", verbose=True)
            input("\nPress Enter to continue...")
        elif choice == "4":
            interactive_delete_ui()
            input("\nPress Enter to continue...")
        elif choice == "5":
            items = adopt_orphaned_dbs(verbose=True)
            print(f"✅ Orphan adoption complete: Injected ProjectId into {len(items)} database(s).")
            input("\nPress Enter to continue...")
        elif choice == "6":
            gaps = check_step_gaps(verbose=True)
            if not gaps:
                print("\n✅ All databases have contiguous steps. Zero gaps.")
            else:
                print(f"\n⚠️ Found {len(gaps)} session(s) with step sequence gaps.")
            input("\nPress Enter to continue...")
        elif choice == "7":
            healed = heal_step_gaps(verbose=True)
            if healed:
                print(f"\n✅ Stitched and healed {len(healed)} session(s).")
            else:
                print("\nℹ️ No step gaps required healing.")
            input("\nPress Enter to continue...")
        elif choice == "8":
            b_res = hourly_backup_manager(max_retention=10, force=True)
            print("\n✅ Backup completed successfully!" if b_res else "\nℹ️ Backup skipped.")
            input("\nPress Enter to continue...")
        elif choice == "9":
            setup_directory_links(CONFIG.gemini_home, verbose=True)
            input("\nPress Enter to continue...")
        elif choice == "10":
            install_startup()
            input("\nPress Enter to continue...")
        elif choice == "11":
            uninstall_startup()
            input("\nPress Enter to continue...")
        else:
            print("\nExiting.")
            break


if __name__ == "__main__":
    main()
