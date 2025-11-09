import asyncio
import json
import logging
import os
import platform
import random
import shutil
import socket
import ssl
import aiohttp
import certifi
from aiohttp import ClientSession, TCPConnector
import decky_plugin

CONFIG_PATH = os.path.join(decky_plugin.DECKY_PLUGIN_SETTINGS_DIR, 'config.json')
ANIMATIONS_PATH = os.path.join(decky_plugin.DECKY_PLUGIN_RUNTIME_DIR, 'animations')
DOWNLOADS_PATH = os.path.join(decky_plugin.DECKY_PLUGIN_RUNTIME_DIR, 'downloads')

# Detect platform and set appropriate Steam paths
def get_steam_paths():
    """Returns tuple of (override_path, steamui_movies_path, steam_root)"""
    system = platform.system()
    steam_root = None
    
    if system == 'Linux':
        steam_root = os.path.expanduser('~/.steam/root')
        override_path = os.path.join(steam_root, 'config', 'uioverrides', 'movies')
        steamui_path = os.path.join(steam_root, 'steamui', 'movies')
        return (override_path, steamui_path, steam_root)
        
    elif system == 'Windows':
        # Try to get Steam path from Windows registry
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam')
            steam_path, _ = winreg.QueryValueEx(key, 'SteamPath')
            winreg.CloseKey(key)
            steam_root = steam_path.replace('/', '\\')
        except:
            # Fallback to common Steam installation locations
            possible_roots = [
                'C:\\Program Files (x86)\\Steam',
                'C:\\Program Files\\Steam',
            ]
            for root in possible_roots:
                if os.path.exists(root):
                    steam_root = root
                    break
        
        if steam_root:
            override_path = os.path.join(steam_root, 'config', 'uioverrides', 'movies')
            steamui_path = os.path.join(steam_root, 'steamui', 'movies')
            return (override_path, steamui_path, steam_root)
            
    else:
        # macOS fallback
        steam_root = os.path.expanduser('~/Library/Application Support/Steam')
        override_path = os.path.join(steam_root, 'config', 'uioverrides', 'movies')
        steamui_path = os.path.join(steam_root, 'steamui', 'movies')
        return (override_path, steamui_path, steam_root)
    
    # Ultimate fallback
    return (None, None, None)

OVERRIDE_PATH, STEAMUI_MOVIES_PATH, STEAM_ROOT = get_steam_paths()

# Platform-specific video filenames
def get_video_names():
    if platform.system() == 'Windows':
        # Windows Steam Big Picture mode
        return {
            'boot': 'bigpicture_startup.webm',
            # Stubs for potential future Windows suspend animation support
            # If Steam adds these in the future, uncomment and update the is_video_supported() function
            'suspend': 'bigpicture_suspend.webm',  # Future: not yet implemented by Steam
            'throbber': 'bigpicture_suspend_from_throbber.webm'  # Future: not yet implemented by Steam
        }
    else:
        # Linux/SteamOS uses deck animations
        return {
            'boot': 'deck_startup.webm',
            'suspend': 'steam_os_suspend.webm',
            'throbber': 'steam_os_suspend_from_throbber.webm'
        }

VIDEO_NAMES_MAP = get_video_names()
BOOT_VIDEO = VIDEO_NAMES_MAP['boot']
SUSPEND_VIDEO = VIDEO_NAMES_MAP['suspend']
THROBBER_VIDEO = VIDEO_NAMES_MAP['throbber']

VIDEOS_NAMES = [BOOT_VIDEO, SUSPEND_VIDEO, THROBBER_VIDEO]
VIDEO_TYPES = ['boot', 'suspend', 'throbber']
VIDEO_TARGETS = ['boot', 'suspend', 'suspend']

# Track which video types are actually used per platform
def is_video_supported(video_type):
    """Check if a video type is supported on current platform"""
    if platform.system() == 'Windows':
        # Windows Big Picture currently only supports boot animation
        # TODO: If Steam adds suspend/throbber support on Windows in the future,
        # update this function to return True for those types and ensure the
        # corresponding filenames exist in steamui/movies
        # For now, only boot animations work
        return video_type == 'boot'
    # Linux/SteamOS supports all types
    return True

REQUEST_RETRIES = 5

ssl_ctx = ssl.create_default_context(cafile=certifi.where())

config = {}
local_animations = []
local_sets = []
animation_cache = []
unloaded = False


async def get_steamdeckrepo():
    try:
        for _ in range(REQUEST_RETRIES):
            async with ClientSession(connector=TCPConnector(family=socket.AF_INET) if config['force_ipv4'] else None) as web:
                async with web.request(
                        'get',
                        f'https://steamdeckrepo.com/api/posts/all',
                        ssl=ssl_ctx
                ) as res:
                    if res.status == 200:
                        data = (await res.json())['posts']
                        break
                    status = res.status
                    if res.status == 429:
                        raise Exception('Rate limit exceeded, try again in a minute')
                    decky_plugin.logger.warning(f'steamdeckrepo fetch failed, status={res.status}')
        else:
            raise Exception(f'Retry attempts exceeded, status code: {status}')
        return [{
            'id': entry['id'],
            'name': entry['title'],
            'preview_image': entry['thumbnail'],
            'preview_video': entry['video'],
            'author': entry['user']['steam_name'],
            'description': entry['content'],
            'last_changed': entry['updated_at'],  # Todo: Ensure consistent date format
            'source': entry['url'],
            'download_url': 'https://steamdeckrepo.com/post/download/' + entry['id'],
            'likes': entry['likes'],
            'downloads': entry['downloads'],
            'version': '',
            'target': 'suspend' if entry['type'] == 'suspend_video' else 'boot',
            'manifest_version': 1
        } for entry in data if entry['type'] in ['suspend_video', 'boot_video']]
    except Exception as e:
        decky_plugin.logger.error('Failed to fetch steamdeckrepo', exc_info=e)
        raise e


async def update_cache():
    global animation_cache
    animation_cache = await get_steamdeckrepo()
    # Todo: JSON URL based sources
    # Todo: How to merge sources with less metadata with steamdeckrepo results gracefully?


async def regenerate_downloads():
    downloads = []
    if len(animation_cache) == 0:
        await update_cache()
    for file in os.listdir(DOWNLOADS_PATH):
        if not file.endswith('.webm'):
            continue
        anim_id = file[:-5]
        for anim in animation_cache:
            if anim['id'] == anim_id:
                downloads.append(anim)
                break
        else:
            decky_plugin.logger.error(f'Failed to find cached entry for id: {anim_id}')
    config['downloads'] = downloads


async def load_config():
    global config
    config = {
        'boot': '',
        'suspend': '',
        'throbber': '',
        'randomize': '',
        'current_set': '',
        'downloads': [],
        'custom_animations': [],
        'custom_sets': [],
        'shuffle_exclusions': [],
        'force_ipv4': False
    }

    async def save_new():
        try:
            await regenerate_downloads()
            save_config()
        except Exception as ex:
            decky_plugin.logger.error('Failed to save new config', exc_info=ex)

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                config.update(json.load(f))
                if type(config['randomize']) == bool:
                    config['randomize'] = ''
        except Exception as e:
            decky_plugin.logger.error('Failed to load config', exc_info=e)
            await save_new()
    else:
        await save_new()


def raise_and_log(msg, ex=None):
    decky_plugin.logger.error(msg, exc_info=ex)
    if ex is None:
        raise Exception(msg)
    raise ex


def save_config():
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        raise_and_log('Failed to save config', e)


def load_local_animations():
    global local_animations
    global local_sets

    animations = []
    sets = []
    directories = next(os.walk(ANIMATIONS_PATH))[1]
    for directory in directories:
        is_set = False
        config_path = os.path.join(ANIMATIONS_PATH, directory, 'config.json')
        anim_config = {}
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    anim_config = json.load(f)
                is_set = True
            except Exception as e:
                decky_plugin.logger.warning(f'Failed to parse config.json for: {directory}', exc_info=e)
        else:
            for video in [BOOT_VIDEO, SUSPEND_VIDEO, THROBBER_VIDEO]:
                if os.path.exists(os.path.join(ANIMATIONS_PATH, directory, video)):
                    is_set = True
                    break
        if not is_set:
            continue

        local_set = {
            'id': directory,
            'enabled': anim_config['enabled'] if 'enabled' in anim_config else True
        }

        def process_animation(default, anim_type, target):
            filename = default if anim_type not in anim_config else anim_config[anim_type]
            if anim_type not in anim_config and not os.path.exists(os.path.join(ANIMATIONS_PATH, directory, filename)):
                filename = ''
            local_set[anim_type] = filename
            if filename != '' and filename is not None:
                animations.append({
                    'id': f'{directory}/{filename}',
                    'name': directory if anim_type == 'boot' else f'{directory} - {anim_type.capitalize()}',
                    'target': target
                })

        for i in range(3):
            process_animation(VIDEOS_NAMES[i], VIDEO_TYPES[i], VIDEO_TARGETS[i])

        sets.append(local_set)

    local_animations = animations
    local_sets = sets


def find_cached_animation(anim_id):
    for anim in animation_cache:
        if anim['id'] == anim_id:
            return anim
    return None


def apply_animation(video, anim_id):
    # Check which video type this is
    video_type = None
    for i, vname in enumerate(VIDEOS_NAMES):
        if vname == video:
            video_type = VIDEO_TYPES[i]
            break
    
    # Skip if not supported on this platform
    if video_type and not is_video_supported(video_type):
        decky_plugin.logger.info(f'Skipping {video_type} animation on {platform.system()} (not supported)')
        return
    
    # On Windows, check if uioverrides works, otherwise use steamui/movies directly
    use_steamui = False
    if platform.system() == 'Windows' and STEAMUI_MOVIES_PATH:
        # Windows Steam doesn't check uioverrides by default, use steamui/movies
        target_path = os.path.join(STEAMUI_MOVIES_PATH, video)
        use_steamui = True
        # Create backup if it doesn't exist
        backup_path = target_path + '.backup'
        if not os.path.exists(backup_path) and os.path.exists(target_path):
            try:
                shutil.copy2(target_path, backup_path)
                decky_plugin.logger.info(f'Created backup: {backup_path}')
            except Exception as e:
                decky_plugin.logger.warning(f'Failed to create backup: {backup_path}', exc_info=e)
    else:
        # Linux/SteamOS uses uioverrides
        target_path = os.path.join(OVERRIDE_PATH, video)
    
    # Remove existing file/symlink if it exists
    if os.path.islink(target_path) or os.path.exists(target_path):
        try:
            os.remove(target_path)
        except Exception as e:
            decky_plugin.logger.warning(f'Failed to remove existing file: {target_path}', exc_info=e)

    if anim_id == '':
        # Restore backup on Windows if reverting to default
        if use_steamui:
            backup_path = target_path + '.backup'
            if os.path.exists(backup_path):
                try:
                    shutil.copy2(backup_path, target_path)
                    decky_plugin.logger.info(f'Restored from backup: {target_path}')
                except Exception as e:
                    decky_plugin.logger.error(f'Failed to restore backup: {backup_path}', exc_info=e)
        return

    path = None
    for anim in config['downloads']:
        if anim['id'] == anim_id:
            path = os.path.join(DOWNLOADS_PATH, f'{anim_id}.webm')
            break
    else:
        for anim in config['custom_animations']:
            if anim['id'] == anim_id:
                path = anim['path']
                break
        else:
            for anim in local_animations:
                if anim['id'] == anim_id:
                    path = os.path.join(ANIMATIONS_PATH, anim_id)
                    break

    if path is None or not os.path.exists(path):
        raise_and_log(f'Failed to find animation for: {anim_id}')

    # Try to create symlink, fallback to copy on Windows if symlink fails
    try:
        os.symlink(path, target_path)
        decky_plugin.logger.info(f'Created symlink: {target_path} -> {path}')
    except (OSError, NotImplementedError) as e:
        # On Windows, symlinks require admin privileges or Developer Mode
        # Fall back to copying the file instead
        if platform.system() == 'Windows' or use_steamui:
            try:
                decky_plugin.logger.info(f'Symlink failed, copying file instead: {path} -> {target_path}')
                shutil.copy2(path, target_path)
            except Exception as copy_error:
                raise_and_log(f'Failed to copy animation file: {path} -> {target_path}', copy_error)
        else:
            raise_and_log(f'Failed to create symlink: {path} -> {target_path}', e)


def apply_animations():
    for i in range(3):
        apply_animation(VIDEOS_NAMES[i], config[VIDEO_TYPES[i]])


def get_active_sets():
    return [entry for entry in local_sets + config['custom_sets'] if entry['enabled']]


def remove_custom_set(set_id):
    config['custom_sets'] = [entry for entry in config['custom_sets'] if entry['id'] != set_id]


def remove_custom_animation(anim_id):
    config['custom_animations'] = [anim for anim in config['custom_animations'] if anim['id'] != anim_id]


def randomize_current_set():
    active = get_active_sets()
    if len(active) > 0:
        new_set = active[random.randint(0, len(active) - 1)]
        config['current_set'] = new_set['id']
        for i in range(3):
            # new_set[VIDEO_TYPES[i]] contains a filename, need to build the full animation ID
            filename = new_set[VIDEO_TYPES[i]]
            if filename:
                # Build full animation ID: 'set_id/filename'
                config[VIDEO_TYPES[i]] = f"{new_set['id']}/{filename}"
            else:
                config[VIDEO_TYPES[i]] = ''
    else:
        # No active sets, clear all animations
        config['current_set'] = ''
        for i in range(3):
            config[VIDEO_TYPES[i]] = ''


def randomize_all():
    for i in range(3):
        pool = [
            anim for anim in local_animations + config['downloads'] + config['custom_animations']
            if anim['target'] == VIDEO_TARGETS[i] and anim['id'] not in config['shuffle_exclusions']
        ]
        if len(pool) > 0:
            config[VIDEO_TYPES[i]] = pool[random.randint(0, len(pool) - 1)]['id']
    config['current_set'] = ''


class Plugin:

    async def getState(self):
        """ Get backend state (animations, sets, and settings) """
        try:
            return {
                'local_animations': local_animations,
                'custom_animations': config['custom_animations'],
                'downloaded_animations': config['downloads'],
                'local_sets': local_sets,
                'custom_sets': config['custom_sets'],
                'settings': {
                    'randomize': config['randomize'],
                    'current_set': config['current_set'],
                    'boot': config['boot'],
                    'suspend': config['suspend'],
                    'throbber': config['throbber'],
                    'shuffle_exclusions': config['shuffle_exclusions'],
                    'force_ipv4': config['force_ipv4']
                }
            }
        except Exception as e:
            decky_plugin.logger.error('Failed to get state', exc_info=e)
            raise e

    async def saveCustomSet(self, set_entry):
        """ Save custom set entry """
        try:
            remove_custom_set(set_entry['id'])
            config['custom_sets'].append(set_entry)
            save_config()
        except Exception as e:
            decky_plugin.logger.error('Failed to save custom set', exc_info=e)
            raise e

    async def removeCustomSet(self, set_id):
        """ Remove custom set """
        try:
            remove_custom_set(set_id)
            save_config()
        except Exception as e:
            decky_plugin.logger.error('Failed to remove custom set', exc_info=e)
            raise e

    async def enableSet(self, set_id, enable):
        """ Enable or disable set """
        try:
            for entry in local_sets:
                if entry['id'] == set_id:
                    entry['enable'] = enable
                    config_file = os.path.join(ANIMATIONS_PATH, entry['id'], 'config.json')
                    with open(config_file, 'w') as f:
                        json.dump(entry, f)
                    return
            for entry in config['custom_sets']:
                if entry['id'] == set_id:
                    entry['enable'] = enable
                    save_config()
                    break
        except Exception as e:
            decky_plugin.logger.error('Failed to enable set', exc_info=e)
            raise e

    async def saveCustomAnimation(self, anim_entry):
        """ Save a custom animation entry """
        try:
            remove_custom_animation(anim_entry['id'])
            config['custom_animations'].append(anim_entry)
            save_config()
        except Exception as e:
            decky_plugin.logger.error('Failed to save custom animation', exc_info=e)
            raise e

    async def removeCustomAnimation(self, anim_id):
        """ Removes custom animation with name """
        try:
            remove_custom_animation(anim_id)
            save_config()
        except Exception as e:
            decky_plugin.logger.error('Failed to remove custom animation', exc_info=e)
            raise e

    async def updateAnimationCache(self):
        """ Update backend animation cache """
        try:
            await update_cache()
        except Exception as e:
            decky_plugin.logger.error('Failed to update animation cache', exc_info=e)
            raise e

    async def getCachedAnimations(self):
        """ Get cached repository animations """
        try:
            return {'animations': animation_cache}
        except Exception as e:
            decky_plugin.logger.error('Failed to get cached animations', exc_info=e)
            raise e

    async def getCachedAnimation(self, anim_id):
        """ Get a cached animation entry for id """
        try:
            return find_cached_animation(anim_id)
        except Exception as e:
            decky_plugin.logger.error('Failed to get cached animations', exc_info=e)
            raise e

    async def downloadAnimation(self, anim_id):
        """ Download a cached animation for id """
        try:
            for entry in config['downloads']:
                if entry['id'] == anim_id:
                    return
            async with aiohttp.ClientSession(connector=TCPConnector(family=socket.AF_INET) if config['force_ipv4'] else None) as web:
                if (anim := find_cached_animation(anim_id)) is None:
                    raise_and_log(f'Failed to find cached animation with id: {anim_id}')
                async with web.get(anim['download_url'], ssl=ssl_ctx) as response:
                    if response.status != 200:
                        raise_and_log(f'Invalid download request status: {response.status}')
                    data = await response.read()
            download_file = os.path.join(DOWNLOADS_PATH, f'{anim_id}.webm')
            with open(download_file, 'wb') as f:
                f.write(data)
            config['downloads'].append(anim)
            save_config()
        except Exception as e:
            decky_plugin.logger.error('Failed to download animation', exc_info=e)
            raise e

    async def deleteAnimation(self, anim_id):
        """ Delete a downloaded animation """
        try:
            config['downloads'] = [entry for entry in config['downloads'] if entry['id'] != anim_id]
            save_config()
            anim_file = os.path.join(DOWNLOADS_PATH, f'{anim_id}.webm')
            if os.path.exists(anim_file):
                os.remove(anim_file)
        except Exception as e:
            decky_plugin.logger.error('Failed to delete animation', exc_info=e)
            raise e

    async def saveSettings(self, settings):
        """ Save settings to config file """
        try:
            config.update(settings)
            save_config()
            apply_animations()
        except Exception as e:
            decky_plugin.logger.error('Failed to save settings', exc_info=e)
            raise e

    async def reloadConfiguration(self):
        """ Reload config file and local animations from disk """
        try:
            await load_config()
            load_local_animations()
            apply_animations()
        except Exception as e:
            decky_plugin.logger.error('Failed to reload configuration', exc_info=e)
            raise e

    async def randomize(self, shuffle):
        """ Randomize animations """
        try:
            if shuffle:
                randomize_all()
            else:
                randomize_current_set()
            save_config()
            apply_animations()
        except Exception as e:
            decky_plugin.logger.error('Failed to randomize animations', exc_info=e)
            raise e

    async def _main(self):
        decky_plugin.logger.info('Initializing...')
        decky_plugin.logger.info(f'Platform: {platform.system()}')
        decky_plugin.logger.info(f'Steam root: {STEAM_ROOT}')
        decky_plugin.logger.info(f'Override path: {OVERRIDE_PATH}')
        decky_plugin.logger.info(f'SteamUI movies path: {STEAMUI_MOVIES_PATH}')

        try:
            os.makedirs(ANIMATIONS_PATH, exist_ok=True)
            if OVERRIDE_PATH:
                os.makedirs(OVERRIDE_PATH, exist_ok=True)
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            os.makedirs(DOWNLOADS_PATH, exist_ok=True)
            decky_plugin.logger.info('Plugin directories created successfully')
        except Exception as e:
            decky_plugin.logger.error('Failed to make plugin directories', exc_info=e)
            raise e

        try:
            await load_config()
            load_local_animations()
        except Exception as e:
            decky_plugin.logger.error('Failed to load config', exc_info=e)
            raise e

        try:
            if config['randomize'] == 'all':
                randomize_all()
            elif config['randomize'] == 'set':
                randomize_current_set()
        except Exception as e:
            decky_plugin.logger.error('Failed to randomize animations', exc_info=e)
            raise e

        try:
            apply_animations()
        except Exception as e:
            decky_plugin.logger.error('Failed to apply animations', exc_info=e)
            raise e

        await asyncio.sleep(5.0)
        if unloaded:
            return
        try:
            await update_cache()
        except Exception as e:
            decky_plugin.logger.error('Failed to update animation cache', exc_info=e)
            raise e

        decky_plugin.logger.info('Initialized')

    async def _unload(self):
        global unloaded
        unloaded = True
        decky_plugin.logger.info('Unloaded')

    async def _migration(self):
        decky_plugin.logger.info('Migrating')
        # `/tmp/animation_changer.log` will be migrated to `decky_plugin.DECKY_PLUGIN_LOG_DIR/template.log`
        decky_plugin.migrate_logs('/tmp/animation_changer.log')
        # `~/.config/AnimationChanger/config.json` will be migrated to `decky_plugin.DECKY_PLUGIN_SETTINGS_DIR/config.json`
        decky_plugin.migrate_settings(os.path.expanduser('~/.config/AnimationChanger/config.json'))
        # `~/homebrew/animations` will be migrated to `decky_plugin.DECKY_PLUGIN_RUNTIME_DIR/animations/`
        decky_plugin.migrate_any(ANIMATIONS_PATH, os.path.expanduser('~/homebrew/animations'))
        # `~/.config/AnimationChanger/downloads` will be migrated to `decky_plugin.DECKY_PLUGIN_RUNTIME_DIR/downloads/`
        decky_plugin.migrate_any(DOWNLOADS_PATH, os.path.expanduser('~/.config/AnimationChanger/downloads'))
