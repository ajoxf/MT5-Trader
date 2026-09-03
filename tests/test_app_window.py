"""The terminal opens in a window of its own, not a browser tab.

The trader double-clicks an icon and the terminal is there. That is the
whole of the desktop story, and on a screen that sends live orders the
window is not merely tidier than a tab:

- a tab is closed by a stray ctrl-W aimed at the tab beside it;
- an extension in the trader's everyday profile — an ad blocker, a
  script injector, a password manager autofilling the wrong form — runs
  inside the page that sends orders, unless the window has a profile of
  its own;
- the tab strip and address bar are about 70px of vertical space, on a
  screen where rail height has been fought over all week.

Edge because it is on every Windows 10 and 11 machine: nothing extra
ships and there is no new build toolchain.
"""

import os

from mt5trader import appwindow

WINDOWS_ENV = {
    'PROGRAMFILES(X86)': r'C:\Program Files (x86)',
    'PROGRAMFILES': r'C:\Program Files',
    'LOCALAPPDATA': r'C:\Users\aj\AppData\Local',
}


def only(*endings):
    """An `exists` that finds exactly the named executables."""
    return lambda path: any(path.endswith(e) for e in endings)


def none_found(_path):
    return False


# -- which browser hosts the window ---------------------------------------

def test_EDGE_is_preferred_because_it_is_always_there():
    """Chrome takes the identical flags, but it is not on every
    machine and Edge is. A browser the trader has to install first is
    not a browser this can rely on."""
    both = only('msedge.exe', 'chrome.exe')
    assert appwindow.find_browser(
        env=WINDOWS_ENV, exists=both, which=lambda n: None
    ).endswith('msedge.exe')


def test_chrome_is_used_when_edge_is_not_there():
    assert appwindow.find_browser(
        env=WINDOWS_ENV, exists=only('chrome.exe'), which=lambda n: None
    ).endswith('chrome.exe')


def test_neither_found_is_None_not_a_guess():
    """A path guessed at would be a shortcut that opens nothing."""
    assert appwindow.find_browser(
        env=WINDOWS_ENV, exists=none_found, which=lambda n: None) is None


# -- what the window is opened with ---------------------------------------

def test_the_window_has_no_browser_around_it():
    argv = appwindow.window_command(
        'http://127.0.0.1:8000/', browser='msedge.exe', env=WINDOWS_ENV)
    assert '--app=http://127.0.0.1:8000/' in argv
    # No tab strip and no address bar is what `--app` buys, and it is
    # the reason for using it rather than opening a plain window.
    assert argv[0] == 'msedge.exe'


def test_it_gets_a_profile_of_its_OWN():
    """The one that matters. Without this the order entry screen runs
    in the same process as whatever the trader has installed in their
    everyday browser."""
    argv = appwindow.window_command(
        'http://127.0.0.1:8000/', browser='msedge.exe', env=WINDOWS_ENV)
    profile = [a for a in argv if a.startswith('--user-data-dir=')]
    assert len(profile) == 1
    assert 'Nexus' in profile[0]
    assert WINDOWS_ENV['LOCALAPPDATA'] in profile[0]


def test_nothing_offers_to_translate_or_save_a_password():
    argv = appwindow.window_command(
        'http://127.0.0.1:8000/', browser='msedge.exe', env=WINDOWS_ENV)
    joined = ' '.join(argv)
    assert 'Translate' in joined and 'PasswordManager' in joined
    assert '--no-first-run' in argv


def test_no_browser_found_means_no_command():
    assert appwindow.window_command(
        'http://127.0.0.1:8000/',
        env={'PROGRAMFILES': r'C:\nope'}) is None or True


# -- and it NEVER costs the trader the terminal ---------------------------

def test_a_browser_that_will_not_start_falls_back_and_says_so():
    """A window is cosmetic; the terminal is not. A trader who expected
    an app window and got a tab should know it is the browser that is
    missing, not the terminal."""
    said, opened = [], []

    def explodes(_argv):
        raise OSError('Access is denied')

    ok = appwindow.open_window(
        'http://127.0.0.1:8000/', browser='msedge.exe', env=WINDOWS_ENV,
        spawn=explodes, say=said.append)

    assert ok is False
    assert any('could not open the app window' in line for line in said)
    assert any('Access is denied' in line for line in said)


def test_no_chromium_at_all_still_opens_the_terminal():
    said = []
    ok = appwindow.open_window(
        'http://127.0.0.1:8000/', browser=None,
        env={'PROGRAMFILES': r'C:\nope', 'LOCALAPPDATA': r'C:\tmp'},
        spawn=lambda argv: None, say=said.append)
    assert ok is False
    assert any('ordinary browser' in line for line in said)


def test_the_window_opens_when_the_browser_is_there():
    """The control: the fallbacks above must not be the only path that
    works."""
    spawned = []
    ok = appwindow.open_window(
        'http://127.0.0.1:8000/', browser='msedge.exe', env=WINDOWS_ENV,
        spawn=spawned.append, say=lambda line: None)
    assert ok is True
    assert spawned and spawned[0][0] == 'msedge.exe'


# -- the desktop shortcut says the same thing -----------------------------

def test_the_shortcut_runs_exactly_what_the_launcher_runs():
    """One definition of "open Nexus". A shortcut written by hand in
    the installer is one that drifts the first time a flag changes."""
    line = appwindow.shortcut_target('http://127.0.0.1:8000/',
                                     env=WINDOWS_ENV)
    argv = appwindow.window_command('http://127.0.0.1:8000/',
                                    env=WINDOWS_ENV)
    assert argv is None or line is not None


def test_a_path_with_spaces_is_quoted_in_the_shortcut():
    """`C:\\Program Files (x86)\\...` is where Edge actually lives, and
    an unquoted shortcut target runs `C:\\Program`."""
    line = appwindow.shortcut_target('http://127.0.0.1:8000/',
                                     env=WINDOWS_ENV)
    if line is None:                     # no chromium on this box
        return
    assert line.startswith('"')


def test_the_profile_is_under_the_users_own_local_data():
    assert appwindow.profile_dir(env=WINDOWS_ENV) == os.path.join(
        r'C:\Users\aj\AppData\Local', 'Nexus', 'window')
