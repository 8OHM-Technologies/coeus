# control_plane/pipelines/views.py
import os
import subprocess

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect

from .models import PipelineConfiguration


def active_pipelines_api(request):
    configs = PipelineConfiguration.objects.filter(is_active=True)
    blueprints = [config.to_blueprint() for config in configs]
    return JsonResponse({"pipelines": blueprints})


def launch_selector_lab(request, pk):
    """
    Triggers 'codegen' inside the Lab container for a specific pipeline.
    Outputs the discovery script to a shared volume.
    """
    config = get_object_or_404(PipelineConfiguration, pk=pk)

    # We save the output to a file named after the pipeline ID
    # This file lives in a volume shared between Django and the Lab container
    discovery_path = f"/app/shared/discovery_{config.pk}.py"

    try:
        # Launch codegen in the 'selector_lab' container (running VNC)
        # We use -d to run it in the background so Django doesn't hang
        subprocess.Popen(
            [
                "docker",
                "exec",
                "-d",
                "coeus_selector_lab",
                "bash",
                "-c",
                f"export DISPLAY=:0 && playwright codegen {config.start_url} --target python -o {discovery_path}",
            ]
        )

        # Redirect the user to the noVNC web interface
        # In a real environment, this would be a Traefik route like lab.coeus.localhost
        return redirect("http://localhost:8081/vnc.html?autoconnect=true")
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def sync_selectors_from_lab(request, pk):
    config = get_object_or_404(PipelineConfiguration, pk=pk)
    discovery_path = f"/app/shared/discovery_{config.pk}.py"

    if not os.path.exists(discovery_path):
        return JsonResponse({"status": "error", "message": "No discovery file found."})

    with open(discovery_path, "r") as f:
        code = f.read()

    config.target_css_selector = code
    config.save()

    return JsonResponse({"status": "success", "message": "Selectors synced!"})
