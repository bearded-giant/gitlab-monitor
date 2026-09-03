# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import os
import re
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List


class RecentProjects:
    """Persist most-recent project paths to ~/.config/gitlab-monitor/recent_projects.json"""

    def __init__(self, config_dir: Path, limit: int = 20):
        self.path = config_dir / "recent_projects.json"
        self.limit = limit
        self._items: List[str] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._items = [str(x) for x in data][:self.limit]
            except Exception:
                self._items = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, 'w') as f:
            json.dump(self._items, f, indent=2)

    def list(self) -> List[str]:
        return list(self._items)

    def remember(self, project_path: str) -> None:
        if not project_path:
            return
        if project_path in self._items:
            self._items.remove(project_path)
        self._items.insert(0, project_path)
        self._items = self._items[:self.limit]
        self._save()

    def remove(self, project_path: str) -> None:
        if project_path in self._items:
            self._items.remove(project_path)
            self._save()


class Favorites:
    """Persist starred project paths to ~/.config/gitlab-monitor/favorites.json"""

    def __init__(self, config_dir: Path):
        self.path = config_dir / "favorites.json"
        self._items: List[str] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._items = [str(x) for x in data]
            except Exception:
                self._items = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, 'w') as f:
            json.dump(self._items, f, indent=2)

    def list(self) -> List[str]:
        return list(self._items)

    def has(self, project_path: str) -> bool:
        return project_path in self._items

    def add(self, project_path: str) -> None:
        if project_path not in self._items:
            self._items.append(project_path)
            self._save()

    def remove(self, project_path: str) -> None:
        if project_path in self._items:
            self._items.remove(project_path)
            self._save()

    def toggle(self, project_path: str) -> bool:
        if self.has(project_path):
            self.remove(project_path)
            return False
        self.add(project_path)
        return True


class MRNotes:
    """Persist local per-MR notes to ~/.config/gitlab-monitor/mr_notes.json.

    Keyed by 'project_path:iid'. Notes are local-only reminders, never synced
    to GitLab.
    """

    def __init__(self, config_dir: Path):
        self.path = config_dir / "mr_notes.json"
        self._notes: Dict[str, Dict[str, str]] = {}
        self._load()

    @staticmethod
    def _key(project_path: str, iid: int) -> str:
        return f"{project_path}:{iid}"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, 'r') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self._notes = {
                        k: v for k, v in data.items()
                        if isinstance(v, dict) and isinstance(v.get('text'), str)
                    }
        except Exception:
            self._notes = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(self._notes, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def get(self, project_path: str, iid: int) -> Optional[str]:
        entry = self._notes.get(self._key(project_path, iid))
        return entry.get('text') if entry else None

    def has(self, project_path: str, iid: int) -> bool:
        return self._key(project_path, iid) in self._notes

    def set(self, project_path: str, iid: int, text: str) -> None:
        if not project_path:
            return
        self._notes[self._key(project_path, iid)] = {
            'text': text,
            'updated_at': datetime.now().isoformat(timespec='seconds'),
        }
        self._save()

    def delete(self, project_path: str, iid: int) -> bool:
        key = self._key(project_path, iid)
        if key in self._notes:
            del self._notes[key]
            self._save()
            return True
        return False


def _slug(text: str, limit: int = 60) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')[:limit] or 'untitled'


class ReviewFiles:
    """Local review files under reviews.dir, one markdown file per MR.

    Frontmatter values are written with json.dumps so the file is valid YAML
    for humans and trivially parseable here without a YAML dependency.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._sig = None
        self._cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _key(project_path: str, iid: int) -> str:
        return f"{project_path}:{iid}"

    @staticmethod
    def parse(path: Path):
        text = path.read_text(encoding='utf-8', errors='replace')
        if not text.startswith('---\n'):
            return {}, text
        end = text.find('\n---\n', 4)
        if end < 0:
            return {}, text
        meta: Dict[str, Any] = {}
        for line in text[4:end].splitlines():
            if ':' not in line:
                continue
            k, v = line.split(':', 1)
            v = v.strip()
            try:
                meta[k.strip()] = json.loads(v)
            except ValueError:
                meta[k.strip()] = v
        return meta, text[end + 5:]

    @staticmethod
    def render(meta: Dict[str, Any], body: str) -> str:
        lines = ['---'] + [f"{k}: {json.dumps(v)}" for k, v in meta.items()] + ['---', '']
        return '\n'.join(lines) + body.rstrip('\n') + '\n'

    def _scan(self):
        if not self.root.exists():
            return ()
        return tuple(sorted(
            (str(p), p.stat().st_mtime_ns)
            for p in self.root.rglob('*.md')
            if not p.name.startswith('.') and '_probe' not in p.parts
        ))

    def index(self) -> Dict[str, Dict[str, Any]]:
        sig = self._scan()
        if sig == self._sig:
            return self._cache
        out: Dict[str, Dict[str, Any]] = {}
        for path_str, _ in sig:
            path = Path(path_str)
            try:
                meta, _body = self.parse(path)
            except OSError:
                continue
            if 'project' not in meta or 'iid' not in meta:
                continue
            meta['path'] = str(path)
            out[self._key(meta['project'], int(meta['iid']))] = meta
        self._sig, self._cache = sig, out
        return out

    def get(self, project_path: str, iid: int) -> Optional[Dict[str, Any]]:
        return self.index().get(self._key(project_path, iid))

    def path_for(self, project_path: str, iid: int, title: str = '') -> Path:
        existing = self.get(project_path, iid)
        if existing and existing.get('path'):
            return Path(existing['path'])
        return self.root / _slug(project_path, 80) / f"{iid}-{_slug(title)}.md"

    def write(self, project_path: str, iid: int, meta: Dict[str, Any], body: str) -> Path:
        path = self.path_for(project_path, iid, meta.get('title', ''))
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {'project': project_path, 'iid': int(iid), **{k: v for k, v in meta.items() if k != 'path'}}
        tmp = path.with_suffix('.md.tmp')
        tmp.write_text(self.render(meta, body), encoding='utf-8')
        os.replace(tmp, path)
        self._sig = None
        return path

    def update_meta(self, project_path: str, iid: int, **fields) -> bool:
        entry = self.get(project_path, iid)
        if not entry:
            return False
        path = Path(entry['path'])
        meta, body = self.parse(path)
        meta.update(fields)
        tmp = path.with_suffix('.md.tmp')
        tmp.write_text(self.render(meta, body), encoding='utf-8')
        os.replace(tmp, path)
        self._sig = None
        return True

    def set_status(self, project_path: str, iid: int, status: str) -> bool:
        return self.update_meta(project_path, iid, status=status)


REVIEWS_DEFAULTS = {
    'enabled': True,
    'dir': '~/dev/reviews',
    'model': 'opus',
    'include_drafts': False,
    'settle_minutes': 10,
}


class Config:
    """Handle configuration from environment variables and config files"""

    def __init__(self):
        self.config_dir = Path.home() / ".config" / "gitlab-monitor"
        self.config_file = self.config_dir / "config.json"
        self.last_view_file = self.config_dir / "last_view.json"
        self._config = self._load_config()
        self.favorites = Favorites(self.config_dir)
        self.recent_projects = RecentProjects(self.config_dir)
        self.mr_notes = MRNotes(self.config_dir)
        self.review_files = ReviewFiles(self.reviews_dir)

    def get_last_view(self) -> Optional[Dict[str, Any]]:
        if not self.last_view_file.exists():
            return None
        try:
            with open(self.last_view_file, 'r') as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get('type'):
                    return data
        except Exception:
            pass
        return None

    def save_last_view(self, view_type: str, **extra) -> None:
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            payload = {'type': view_type, **extra}
            with open(self.last_view_file, 'w') as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass

    def clear_last_view(self) -> None:
        try:
            if self.last_view_file.exists():
                self.last_view_file.unlink()
        except Exception:
            pass
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from file and environment variables"""
        config = {
            'gitlab_url': None,
            'gitlab_token': None,
            'project_path': None,
            'refresh_interval': 30,
            'max_pipelines': 50,
            'theme': 'dark',
            'reviews': dict(REVIEWS_DEFAULTS),
        }
        
        # Load from config file if it exists
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    file_config = json.load(f)
                    file_reviews = file_config.pop('reviews', None)
                    config.update(file_config)
                    if isinstance(file_reviews, dict):
                        config['reviews'].update(file_reviews)
            except Exception:
                pass
        
        # Environment variables override config file
        if os.environ.get('GITLAB_URL'):
            config['gitlab_url'] = os.environ['GITLAB_URL']
        if os.environ.get('GITLAB_TOKEN'):
            config['gitlab_token'] = os.environ['GITLAB_TOKEN']
        elif not config.get('gitlab_token'):
            # launchd gives the poller a bare env, so a 0600 token file is the non-interactive path
            token_file = self.config_dir / 'token'
            try:
                if token_file.exists():
                    config['gitlab_token'] = token_file.read_text().strip() or None
            except OSError:
                pass
        if os.environ.get('GITLAB_PROJECT'):
            config['project_path'] = os.environ['GITLAB_PROJECT']
        if os.environ.get('GITLAB_REFRESH_INTERVAL'):
            try:
                config['refresh_interval'] = int(os.environ['GITLAB_REFRESH_INTERVAL'])
            except ValueError:
                pass
        if os.environ.get('GLMON_REVIEWS_DIR'):
            config['reviews']['dir'] = os.environ['GLMON_REVIEWS_DIR']
        if os.environ.get('GLMON_REVIEWS_ENABLED'):
            config['reviews']['enabled'] = os.environ['GLMON_REVIEWS_ENABLED'].lower() not in ('0', 'false', 'no', 'off')
        
        return config
    
    def save_config(self, **kwargs):
        """Save configuration to file"""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        
        # Update current config
        self._config.update(kwargs)
        
        # Don't save token to file for security
        config_to_save = {k: v for k, v in self._config.items() if k != 'gitlab_token'}
        
        with open(self.config_file, 'w') as f:
            json.dump(config_to_save, f, indent=2)
    
    @property
    def gitlab_url(self) -> Optional[str]:
        return self._config.get('gitlab_url')
    
    @property
    def gitlab_token(self) -> Optional[str]:
        return self._config.get('gitlab_token')
    
    @property
    def project_path(self) -> Optional[str]:
        return self._config.get('project_path')
    
    @property
    def refresh_interval(self) -> int:
        return self._config.get('refresh_interval', 30)
    
    @property
    def max_pipelines(self) -> int:
        return self._config.get('max_pipelines', 50)

    @property
    def export_dir(self) -> str:
        return self._config.get('export_dir') or str(Path.home())

    def set_export_dir(self, path: str) -> None:
        self.save_config(export_dir=path)

    @property
    def reviews(self) -> Dict[str, Any]:
        merged = dict(REVIEWS_DEFAULTS)
        merged.update(self._config.get('reviews') or {})
        return merged

    @property
    def reviews_dir(self) -> Path:
        return Path(os.path.expanduser(str(self.reviews.get('dir') or REVIEWS_DEFAULTS['dir'])))

    @property
    def reviews_enabled(self) -> bool:
        return bool(self.reviews.get('enabled', True))

    def set_reviews_enabled(self, enabled: bool) -> None:
        reviews = self.reviews
        reviews['enabled'] = bool(enabled)
        self.save_config(reviews=reviews)
    
    def validate(self) -> tuple[bool, str]:
        """Validate required configuration (project_path is optional now)"""
        if not self.gitlab_url:
            return False, "GITLAB_URL not set. Set via environment variable or config file"
        if not self.gitlab_token:
            return False, "GITLAB_TOKEN not set. Set via environment variable"
        return True, "Configuration valid"