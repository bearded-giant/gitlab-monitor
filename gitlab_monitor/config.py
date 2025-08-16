# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import os
import json
from pathlib import Path
from typing import Optional, Dict, Any


class Config:
    """Handle configuration from environment variables and config files"""
    
    def __init__(self):
        self.config_dir = Path.home() / ".config" / "gitlab-monitor"
        self.config_file = self.config_dir / "config.json"
        self._config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from file and environment variables"""
        config = {
            'gitlab_url': None,
            'gitlab_token': None,
            'project_path': None,
            'refresh_interval': 30,
            'max_pipelines': 50,
            'theme': 'dark',
        }
        
        # Load from config file if it exists
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    file_config = json.load(f)
                    config.update(file_config)
            except Exception:
                pass
        
        # Environment variables override config file
        if os.environ.get('GITLAB_URL'):
            config['gitlab_url'] = os.environ['GITLAB_URL']
        if os.environ.get('GITLAB_TOKEN'):
            config['gitlab_token'] = os.environ['GITLAB_TOKEN']
        if os.environ.get('GITLAB_PROJECT'):
            config['project_path'] = os.environ['GITLAB_PROJECT']
        if os.environ.get('GITLAB_REFRESH_INTERVAL'):
            try:
                config['refresh_interval'] = int(os.environ['GITLAB_REFRESH_INTERVAL'])
            except ValueError:
                pass
        
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
    
    def validate(self) -> tuple[bool, str]:
        """Validate required configuration"""
        if not self.gitlab_url:
            return False, "GITLAB_URL not set. Set via environment variable or config file"
        if not self.gitlab_token:
            return False, "GITLAB_TOKEN not set. Set via environment variable"
        if not self.project_path:
            return False, "GITLAB_PROJECT not set. Set via environment variable or config file"
        return True, "Configuration valid"