from typing import Optional


Contextual = Optional
""" Contextual is a type alias for Optional[Any] , it is used to declare that
a variable is set within  the context of this class, but will be none outside"""

ContextBool = bool
""" ContextBool is a type alias for bool , it is used to declare that this boolean
can change within the context of this class, but will be constant outside"""
