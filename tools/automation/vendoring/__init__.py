""""Vendoring script, python 3.5 with requests needed"""

# The following comment should be removed at some point in the future.
# mypy: disallow-untyped-defs=False

import re
import shutil
import tarfile
import zipfile
from pathlib import Path

import invoke
import requests

from .typing import generate_stubs

FILE_WHITE_LIST = (
    'Makefile',
    'vendor.txt',
    '__init__.py',
    'README.rst',
)

# libraries that have directories with different names
LIBRARY_DIRNAMES = {
    'setuptools': 'pkg_resources',
    'msgpack-python': 'msgpack',
}

# from time to time, remove the no longer needed ones
_github_license = "https://github.com/{}/raw/master/LICENSE"
HARDCODED_LICENSE_URLS = {
    'pytoml': _github_license.format('avakar/pytoml'),
    'webencodings': _github_license.format('SimonSapin/python-webencodings')
}


def drop_dir(path, **kwargs):
    shutil.rmtree(str(path), **kwargs)


def remove_all(paths):
    for path in paths:
        if path.is_dir():
            drop_dir(path)
        else:
            path.unlink()


def log(msg):
    print('[vendoring.update] ' + msg)


def _get_vendor_dir(ctx):
    git_root = ctx.run('git rev-parse --show-toplevel', hide=True).stdout
    return Path(git_root.strip()) / 'src' / 'pip' / '_vendor'


def clean_vendor(ctx, vendor_dir):
    # Old _vendor cleanup
    remove_all(vendor_dir.glob('*.pyc'))
    log('Cleaning %s' % vendor_dir)
    for item in vendor_dir.iterdir():
        if item.is_dir():
            shutil.rmtree(str(item))
        elif item.name not in FILE_WHITE_LIST:
            item.unlink()
        else:
            log('Skipping %s' % item)


def detect_vendored_libs(vendor_dir):
    retval = []
    for item in vendor_dir.iterdir():
        if item.is_dir():
            retval.append(item.name)
        elif item.name.endswith(".pyi"):
            continue
        elif "LICENSE" in item.name or "COPYING" in item.name:
            continue
        elif item.name not in FILE_WHITE_LIST:
            retval.append(item.name[:-3])
    return retval


def rewrite_imports(package_dir, vendored_libs):
    for item in package_dir.iterdir():
        if item.is_dir():
            rewrite_imports(item, vendored_libs)
        elif item.name.endswith('.py'):
            rewrite_file_imports(item, vendored_libs)


def rewrite_file_imports(item, vendored_libs):
    """Rewrite 'import xxx' and 'from xxx import' for vendored_libs"""
    text = item.read_text(encoding='utf-8')
    # Revendor pkg_resources.extern first
    text = re.sub(r'pkg_resources\.extern', r'pip._vendor', text)
    text = re.sub(r'from \.extern', r'from pip._vendor', text)
    for lib in vendored_libs:
        text = re.sub(
            r'(\n\s*|^)import %s(\n\s*)' % lib,
            r'\1from pip._vendor import %s\2' % lib,
            text,
        )
        text = re.sub(
            r'(\n\s*|^)from %s(\.|\s+)' % lib,
            r'\1from pip._vendor.%s\2' % lib,
            text,
        )
    item.write_text(text, encoding='utf-8')


def apply_patch(ctx, patch_file_path):
    log('Applying patch %s' % patch_file_path.name)
    ctx.run('git apply --verbose %s' % patch_file_path)


def vendor(ctx, vendor_dir):
    log('Reinstalling vendored libraries')
    # We use --no-deps because we want to ensure that all of our dependencies
    # are added to vendor.txt, this includes all dependencies recursively up
    # the chain.
    ctx.run(
        'pip install -t {0} -r {0}/vendor.txt --no-compile --no-deps'.format(
            str(vendor_dir),
        )
    )
    remove_all(vendor_dir.glob('*.dist-info'))
    remove_all(vendor_dir.glob('*.egg-info'))

    # Cleanup setuptools unneeded parts
    (vendor_dir / 'easy_install.py').unlink()
    drop_dir(vendor_dir / 'setuptools')
    drop_dir(vendor_dir / 'pkg_resources' / '_vendor')
    drop_dir(vendor_dir / 'pkg_resources' / 'extern')

    # Drop the bin directory (contains easy_install, distro, chardetect etc.)
    # Might not appear on all OSes, so ignoring errors
    drop_dir(vendor_dir / 'bin', ignore_errors=True)

    # Drop interpreter and OS specific msgpack libs.
    # Pip will rely on the python-only fallback instead.
    remove_all(vendor_dir.glob('msgpack/*.so'))

    # Detect the vendored packages/modules
    vendored_libs = detect_vendored_libs(vendor_dir)
    log("Detected vendored libraries: %s" % ", ".join(vendored_libs))

    # Global import rewrites
    log("Rewriting all imports related to vendored libs")
    for item in vendor_dir.iterdir():
        if item.is_dir():
            rewrite_imports(item, vendored_libs)
        elif item.name not in FILE_WHITE_LIST:
            rewrite_file_imports(item, vendored_libs)

    # Special cases: apply stored patches
    log("Apply patches")
    patch_dir = Path(__file__).parent / 'patches'
    for patch in patch_dir.glob('*.patch'):
        apply_patch(ctx, patch)


def download_licenses(ctx, vendor_dir):
    log('Downloading licenses')
    tmp_dir = vendor_dir / '__tmp__'
    ctx.run(
        'pip download -r {0}/vendor.txt --no-binary '
        ':all: --no-deps -d {1}'.format(
            str(vendor_dir),
            str(tmp_dir),
        )
    )
    for sdist in tmp_dir.iterdir():
        extract_license(vendor_dir, sdist)
    drop_dir(tmp_dir)


def extract_license(vendor_dir, sdist):
    if sdist.suffixes[-2] == '.tar':
        ext = sdist.suffixes[-1][1:]
        with tarfile.open(sdist, mode='r:{}'.format(ext)) as tar:
            found = find_and_extract_license(vendor_dir, tar, tar.getmembers())
    elif sdist.suffixes[-1] == '.zip':
        with zipfile.ZipFile(sdist) as zip:
            found = find_and_extract_license(vendor_dir, zip, zip.infolist())
    else:
        raise NotImplementedError('new sdist type!')

    if not found:
        log('License not found in {}, will download'.format(sdist.name))
        license_fallback(vendor_dir, sdist.name)


def find_and_extract_license(vendor_dir, tar, members):
    found = False
    for member in members:
        try:
            name = member.name
        except AttributeError:  # zipfile
            name = member.filename
        if 'LICENSE' in name or 'COPYING' in name:
            if '/test' in name:
                # some testing licenses in html5lib and distlib
                log('Ignoring {}'.format(name))
                continue
            found = True
            extract_license_member(vendor_dir, tar, member, name)
    return found


def license_fallback(vendor_dir, sdist_name):
    """Hardcoded license URLs. Check when updating if those are still needed"""
    libname = libname_from_dir(sdist_name)
    if libname not in HARDCODED_LICENSE_URLS:
        raise ValueError('No hardcoded URL for {} license'.format(libname))

    url = HARDCODED_LICENSE_URLS[libname]
    _, _, name = url.rpartition('/')
    dest = license_destination(vendor_dir, libname, name)
    log('Downloading {}'.format(url))
    r = requests.get(url, allow_redirects=True)
    r.raise_for_status()
    dest.write_bytes(r.content)


def libname_from_dir(dirname):
    """Reconstruct the library name without it's version"""
    parts = []
    for part in dirname.split('-'):
        if part[0].isdigit():
            break
        parts.append(part)
    return '-'.join(parts)


def license_destination(vendor_dir, libname, filename):
    """Given the (reconstructed) library name, find appropriate destination"""
    normal = vendor_dir / libname
    if normal.is_dir():
        return normal / filename
    lowercase = vendor_dir / libname.lower()
    if lowercase.is_dir():
        return lowercase / filename
    if libname in LIBRARY_DIRNAMES:
        return vendor_dir / LIBRARY_DIRNAMES[libname] / filename
    # fallback to libname.LICENSE (used for nondirs)
    return vendor_dir / '{}.{}'.format(libname, filename)


def extract_license_member(vendor_dir, tar, member, name):
    mpath = Path(name)  # relative path inside the sdist
    dirname = list(mpath.parents)[-2].name  # -1 is .
    libname = libname_from_dir(dirname)
    dest = license_destination(vendor_dir, libname, mpath.name)
    dest_relative = dest.relative_to(Path.cwd())
    log('Extracting {} into {}'.format(name, dest_relative))
    try:
        fileobj = tar.extractfile(member)
        dest.write_bytes(fileobj.read())
    except AttributeError:  # zipfile
        dest.write_bytes(tar.read(member))


@invoke.task
def update_stubs(ctx):
    vendor_dir = _get_vendor_dir(ctx)
    vendored_libs = detect_vendored_libs(vendor_dir)

    print("[vendoring.update_stubs] Add mypy stubs")
    generate_stubs(vendor_dir, vendored_libs)


@invoke.task(name="update", post=[update_stubs])
def main(ctx):
    vendor_dir = _get_vendor_dir(ctx)
    log('Using vendor dir: %s' % vendor_dir)
    clean_vendor(ctx, vendor_dir)
    vendor(ctx, vendor_dir)
    download_licenses(ctx, vendor_dir)
    log('Revendoring complete')
