class EventDispatcher:
    def __init__(self):
        self._listeners = {}

    def on(self, event_name):
        def decorator(func):
            if event_name not in self._listeners:
                self._listeners[event_name] = []
            self._listeners[event_name].append(func)
            return func

        return decorator

    def emit(self, event_name, *args, **kwargs):
        if not event_name in self._listeners:
            return

        for listener in self._listeners[event_name]:
            try:
                listener(*args, **kwargs)
            except Exception as e:
                print(f"Error in listener for {event_name}: {e}")
