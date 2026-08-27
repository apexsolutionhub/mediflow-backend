from django.db.models import Count
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import SalesAgent


class SalesAgentSerializer(serializers.ModelSerializer):
    displayName = serializers.CharField(source="display_name", max_length=255)
    isActive = serializers.BooleanField(source="is_active", required=False)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    tenantCount = serializers.IntegerField(read_only=True)

    class Meta:
        model = SalesAgent
        fields = [
            "id",
            "displayName",
            "phone",
            "email",
            "city",
            "notes",
            "isActive",
            "createdAt",
            "tenantCount",
        ]

    def to_internal_value(self, data):
        payload = dict(data)
        if "id" in payload:
            payload.pop("id", None)
        return super().to_internal_value(payload)


def annotated_agents():
    return SalesAgent.objects.annotate(tenantCount=Count("tenants"))


def resolve_sales_agent(sales_agent_id):
    if sales_agent_id in (None, "", 0, "0"):
        return None
    try:
        pk = int(sales_agent_id)
    except (TypeError, ValueError) as exc:
        raise serializers.ValidationError({"sales_agent_id": ["Invalid sales agent."]}) from exc
    agent = SalesAgent.objects.filter(pk=pk, is_active=True).first()
    if agent is None:
        raise serializers.ValidationError({"sales_agent_id": ["Unknown or inactive sales agent."]})
    return agent


class PublicSalesAgentsView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        qs = annotated_agents().filter(is_active=True).order_by("display_name")
        return Response(SalesAgentSerializer(qs, many=True).data)
