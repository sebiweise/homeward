SPK_NAME = homeward
SPK_VERS = 0.1.0
SPK_REV = 1
SPK_ICON = src/homeward.png

PYTHON_PACKAGE = python312
WHEELS = src/requirements-crossenv.txt src/requirements-pure.txt

# The DSM app (ui/); the Python code comes from PyPI (requirements-pure.txt).
DEPENDS = cross/homeward

# The DSM app uses DSM 7.2 web APIs.
REQUIRED_MIN_DSM = 7.2

MAINTAINER = sebiweise
DESCRIPTION = Backs up iCloud Photos to the NAS and syncs iCloud Drive folders \(upload, download or bidirectional\). With a DSM app for accounts, sync pairs, status and log.
DESCRIPTION_GER = Sichert iCloud-Fotos auf der NAS und synchronisiert iCloud-Drive-Ordner \(Upload, Download oder bidirektional\). Mit DSM-App für Konten, Sync-Paare, Status und Protokoll.
DISPLAY_NAME = Homeward
CHANGELOG = "Initial package release."
HOMEPAGE = https://github.com/sebiweise/homeward
SUPPORTURL = https://github.com/sebiweise/homeward/issues
LICENSE = MIT
STARTABLE = yes

BETA = 1
REPORT_URL = https://github.com/sebiweise/homeward/issues

WIZARDS_DIR = src/wizard/

SERVICE_USER = auto
SERVICE_SETUP = src/service-setup.sh
SERVICE_WIZARD_SHARENAME = wizard_shared_folder_name

# Native DSM app (Ext.js) shipped by cross/homeward
DSM_UI_DIR = ui
DSM_APP_NAME = SYNO.SDS.Homeward.Instance

SPK_USR_LOCAL_LINKS = bin:env/bin/homeward

USE_ALTERNATE_TMPDIR = 1

include ../../mk/spksrc.spk-meta.mk
