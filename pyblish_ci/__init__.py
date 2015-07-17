import logging

formatter = logging.Formatter("%(levelname)s - %(message)s")
handler = logging.StreamHandler()
handler.setFormatter(formatter)

log = logging.getLogger()
log.propagate = True
log.handlers[:] = []
log.addHandler(handler)
log.setLevel(logging.DEBUG)
