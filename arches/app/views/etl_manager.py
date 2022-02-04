import json
import logging
# from django.core.exceptions import ObjectDoesNotExist
# from django.db import transaction
# from django.utils.translation import ugettext as _
from django.views.generic import View
from arches.app.models.models import ETLModule
# from arches.app.utils.betterJSONSerializer import JSONSerializer, JSONDeserializer
from arches.app.utils.response import JSONResponse

logger = logging.getLogger(__name__)

class ETLManagerView(View):
    """
    to get the ETL modules from db
    """
    def get(self, request):
        etlmodules = ETLModule.objects.all()
        return JSONResponse(etlmodules)

    def post(self, request):
        """
        instantiate the proper module with proper action and pass the request
        possible actions are "import", "validate", "return first line", ""
        """
        action = request.POST.get('action')
        module = request.POST.get('module')
        import_module = ETLModule.objects.get(slug=module).get_class_module()()
        import_function = getattr(import_module, action)
        ret = {'result': import_function(request=request)}
        return JSONResponse(ret)
