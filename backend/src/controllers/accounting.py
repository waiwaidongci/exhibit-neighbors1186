from ..services import accounting


class AccountingController:
    terminals = staticmethod(accounting.list_terminals)
    create_terminal = staticmethod(accounting.create_terminal)
    add_events = staticmethod(accounting.add_events)
    status = staticmethod(accounting.status)
    events = staticmethod(accounting.list_events)
    close = staticmethod(accounting.close_books)
    versions = staticmethod(accounting.list_versions)
    version = staticmethod(accounting.get_version)
    recompute = staticmethod(accounting.recompute_version)
