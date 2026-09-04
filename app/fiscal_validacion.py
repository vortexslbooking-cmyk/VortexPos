"""
Validación de identificadores fiscales españoles y de los datos mínimos que
exige una factura completa.

POR QUÉ ESTO VIVE EN EL SERVIDOR
--------------------------------
La comprobación también se hace en la app, para avisar al camarero mientras
teclea. Pero la que MANDA es esta: la app es un HTML en una tablet y cualquiera
puede saltársela. Una factura completa con un NIF inventado es un problema del
bar frente a Hacienda, no un fallo de interfaz.

QUÉ COMPRUEBA Y QUÉ NO
----------------------
Comprueba que el identificador está BIEN FORMADO: la letra de control de un NIF
o de un NIE, y el dígito de control de un CIF, se calculan con un algoritmo y o
cuadran o no cuadran. Eso pilla el 90 % de los errores de teclado.

Lo que NO puede hacer es decir si ese NIF EXISTE de verdad o a quién pertenece.
Para eso hace falta consultar el censo de la AEAT, que es otra cosa y requiere
certificado. Aquí no se finge: si el formato cuadra, se acepta.
"""
import re
from typing import Dict, List, Optional, Tuple

# Letra de control del NIF/NIE. El orden importa: es el resto de dividir el
# número entre 23 lo que da la posición en esta cadena.
_LETRAS_NIF = "TRWAGMYFPDXBNJZSQVHLCKE"

# Primer carácter de un CIF y qué tipo de entidad es. Se guarda la descripción
# porque al bar le sirve para entender qué le están dando.
_CIF_TIPOS = {
    "A": "Sociedad anónima",
    "B": "Sociedad de responsabilidad limitada",
    "C": "Sociedad colectiva",
    "D": "Sociedad comanditaria",
    "E": "Comunidad de bienes",
    "F": "Sociedad cooperativa",
    "G": "Asociación",
    "H": "Comunidad de propietarios",
    "J": "Sociedad civil",
    "N": "Entidad extranjera",
    "P": "Corporación local",
    "Q": "Organismo público",
    "R": "Congregación o institución religiosa",
    "S": "Órgano de la Administración",
    "U": "Unión temporal de empresas",
    "V": "Otro tipo sin personalidad jurídica",
    "W": "Establecimiento permanente de entidad no residente",
}

# Las que llevan letra de control en vez de dígito. Para el resto vale
# cualquiera de las dos formas, así que se aceptan ambas.
_CIF_LETRA_OBLIGATORIA = set("KPQRSNW")
_CIF_DIGITO_OBLIGATORIO = set("ABEH")


def normaliza(valor: Optional[str]) -> str:
    """Mayúsculas y sin espacios, guiones ni puntos. '12345678-z' -> '12345678Z'."""
    return re.sub(r"[^0-9A-Z]", "", (valor or "").upper())


def _letra_nif(numero: int) -> str:
    return _LETRAS_NIF[numero % 23]


def valida_nif(valor: str) -> bool:
    """DNI de una persona física: 8 dígitos y su letra de control."""
    v = normaliza(valor)
    if not re.fullmatch(r"\d{8}[A-Z]", v):
        return False
    return v[8] == _letra_nif(int(v[:8]))


def valida_nie(valor: str) -> bool:
    """Extranjero residente: X/Y/Z + 7 dígitos + letra. La inicial vale 0, 1 o 2."""
    v = normaliza(valor)
    if not re.fullmatch(r"[XYZ]\d{7}[A-Z]", v):
        return False
    numero = int(str("XYZ".index(v[0])) + v[1:8])
    return v[8] == _letra_nif(numero)


def valida_cif(valor: str) -> bool:
    """
    CIF de una entidad: letra + 7 dígitos + control.

    El control sale de sumar los dígitos en posición impar y, en las pares,
    duplicarlos sumando las cifras del resultado. Es el mismo algoritmo que
    publica la AEAT.
    """
    v = normaliza(valor)
    if not re.fullmatch(r"[A-W]\d{7}[0-9A-J]", v):
        return False
    inicial, cuerpo, control = v[0], v[1:8], v[8]

    suma_par = sum(int(c) for c in cuerpo[1::2])          # posiciones 2,4,6
    suma_impar = 0
    for c in cuerpo[0::2]:                                # posiciones 1,3,5,7
        doble = int(c) * 2
        suma_impar += doble // 10 + doble % 10
    digito = (10 - (suma_par + suma_impar) % 10) % 10

    if inicial in _CIF_LETRA_OBLIGATORIA:
        return control == "JABCDEFGHI"[digito]
    if inicial in _CIF_DIGITO_OBLIGATORIO:
        return control == str(digito)
    # El resto admite las dos formas.
    return control == str(digito) or control == "JABCDEFGHI"[digito]


def identifica(valor: str) -> Tuple[bool, str, str]:
    """
    Devuelve (es_valido, clase, explicacion).

    `clase` es "nif", "nie", "cif" o "" si no cuadra con ninguno. La explicación
    va escrita para que se pueda enseñar tal cual al camarero.
    """
    v = normaliza(valor)
    if not v:
        return False, "", "Falta el NIF o CIF."
    if len(v) != 9:
        return False, "", "Un NIF o CIF español tiene 9 caracteres; este tiene %d." % len(v)

    if re.fullmatch(r"\d{8}[A-Z]", v):
        if valida_nif(v):
            return True, "nif", "NIF de persona física."
        return False, "nif", ("La letra del NIF no corresponde. Para %s la letra "
                              "sería %s." % (v[:8], _letra_nif(int(v[:8]))))

    if re.fullmatch(r"[XYZ]\d{7}[A-Z]", v):
        if valida_nie(v):
            return True, "nie", "NIE de extranjero residente."
        return False, "nie", "La letra del NIE no corresponde."

    if re.fullmatch(r"[A-W]\d{7}[0-9A-J]", v):
        if valida_cif(v):
            tipo = _CIF_TIPOS.get(v[0], "Entidad")
            return True, "cif", "CIF de entidad: %s." % tipo
        return False, "cif", "El dígito de control del CIF no corresponde."

    return False, "", "No tiene forma de NIF, NIE ni CIF español."


# --------------------------------------------------------------------------
# Datos mínimos de una factura completa
# --------------------------------------------------------------------------
# Lo que exige el art. 6.1 del RD 1619/2012 sobre el DESTINATARIO: nombre o
# razón social, NIF y domicilio. El resto de datos obligatorios de la factura
# (número, serie, fecha, datos del emisor, desglose) los pone el sistema, no
# el camarero, así que no se piden aquí.
_OBLIGATORIOS = [
    ("nombre", "el nombre o la razón social"),
    ("nif", "el NIF o CIF"),
    ("direccion", "la dirección fiscal"),
    ("cp", "el código postal"),
    ("municipio", "el municipio"),
]

_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def valida_cliente(cliente: Optional[Dict]) -> List[str]:
    """
    Devuelve la lista de problemas. Lista vacía = se puede facturar.

    Se devuelven TODOS los fallos de una vez, no el primero: obligar a alguien
    a corregir de uno en uno, con una cola esperando en la barra, es la forma
    más rápida de que deje de usar la factura completa.
    """
    c = cliente or {}
    fallos: List[str] = []

    for campo, comose_llama in _OBLIGATORIOS:
        if not str(c.get(campo) or "").strip():
            fallos.append("Falta %s." % comose_llama)

    nif = str(c.get("nif") or "").strip()
    if nif:
        ok, _clase, explicacion = identifica(nif)
        if not ok:
            fallos.append(explicacion)

    cp = normaliza(c.get("cp"))
    if cp:
        pais = (str(c.get("pais") or "ES").strip().upper() or "ES")
        # El código postal solo se comprueba en España: fuera, cada país tiene
        # su formato y no vamos a inventarnos reglas.
        if pais == "ES":
            if not re.fullmatch(r"\d{5}", cp):
                fallos.append("El código postal debe tener 5 dígitos.")
            elif not (1 <= int(cp[:2]) <= 52):
                fallos.append("Los dos primeros dígitos del código postal (%s) "
                              "no son una provincia española." % cp[:2])

    email = str(c.get("email") or "").strip()
    if email and not _RE_EMAIL.match(email):
        fallos.append("La dirección de correo no tiene un formato válido.")

    tipo = str(c.get("tipo") or "").strip().lower()
    if tipo and tipo not in TIPOS_CLIENTE:
        fallos.append("Tipo de cliente desconocido: %s." % tipo)

    return fallos


# Tipos de cliente. Es una lista abierta a propósito: añadir "administracion"
# el día que un ayuntamiento pida factura no debe obligar a tocar el esquema,
# solo esta constante y el desplegable de la app.
TIPOS_CLIENTE = {
    "particular": "Particular",
    "autonomo": "Autónomo",
    "empresa": "Empresa / S.L.",
}
