from django.contrib import admin

from . import models

for model in (
    models.Patient,
    models.Encounter,
    models.BillableService,
    models.Medicine,
    models.Department,
    models.EquipmentTicket,
):
    admin.site.register(model)
