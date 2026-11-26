"""WhatsApp is an extraction source: there is no API to discover WhatsApp contacts.

Only `wa.me/<number>` or `api.whatsapp.com/send?phone=<number>` links that are literally published on a prospect's
public page are returned. A phone number is never turned into a WhatsApp link: a public number is not proof the
person uses WhatsApp, so that would be generating contact information.
"""

from app.utils.validators import find_whatsapp

NAME = "WhatsApp"
METHOD = "extraction"

__all__ = ["NAME", "METHOD", "find_whatsapp"]
