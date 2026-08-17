"""Random Brazilian payer names and tax ids for the sandbox."""

import random
from dataclasses import dataclass

from app.utils import cpf

FIRST_NAMES = [
    "Ana", "Bruno", "Carla", "Diego", "Eduarda", "Felipe", "Gabriela",
    "Henrique", "Isabela", "Joao", "Karina", "Lucas", "Mariana", "Nicolas",
    "Olivia", "Paulo", "Rafaela", "Samuel", "Tatiana", "Vinicius",
]

LAST_NAMES = [
    "Almeida", "Barbosa", "Cardoso", "Duarte", "Esteves", "Ferreira",
    "Gomes", "Henriques", "Ibrahim", "Junqueira", "Klein", "Lima",
    "Machado", "Nogueira", "Oliveira", "Pereira", "Queiroz", "Ribeiro",
    "Santos", "Teixeira",
]


@dataclass(frozen=True)
class Payer:
    name: str
    tax_id: str


def random_payer(rng: random.Random | None = None) -> Payer:
    rng = rng or random
    name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
    return Payer(name=name, tax_id=cpf.generate(rng))
