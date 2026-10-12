"""Wires the handlers into an :class:`~homeward.api.server.Api`."""

from homeward.api.accounts import AccountBackend
from homeward.api.handlers import accounts, log, pairs, photos, status
from homeward.api.server import Api, Context
from homeward.api.store import ConfigStore
from homeward.runtime import Paths


def build_api(paths: Paths, backend: AccountBackend) -> Api:
    api = Api(Context(paths=paths, store=ConfigStore(paths), accounts=backend))
    api.add("status.get", status.get)
    api.add("log.tail", log.tail)
    api.add("log.runs", log.runs)
    api.add("log.events", log.events)
    api.add("pairs.list", pairs.list_pairs)
    api.add("pairs.create", pairs.create, write=True)
    api.add("pairs.update", pairs.update, write=True)
    api.add("pairs.delete", pairs.delete, write=True)
    api.add("pairs.run", pairs.run, write=True)
    api.add("photos.defaults", photos.defaults)
    api.add("photos.list", photos.list_jobs)
    api.add("photos.create", photos.create, write=True)
    api.add("photos.update", photos.update, write=True)
    api.add("photos.delete", photos.delete, write=True)
    api.add("photos.run", photos.run, write=True)
    api.add("accounts.list", accounts.list_accounts)
    api.add("accounts.add", accounts.add, write=True)
    api.add("accounts.login", accounts.login, write=True)
    api.add("accounts.verify", accounts.verify, write=True)
    api.add("accounts.remove", accounts.remove, write=True)
    return api
