"use strict";
// Reviewed callable routine: managed reader callback and projection share this episode.
return function createNormalRoutine(adapter) {
  if (!adapter || adapter.profile !== "FRESH_NATIVE_SUPPORTED_ORCHESTRATION/v2")
    throw new Error("ADAPTER_PROFILE_MISMATCH");
  return async function normalRead({request, managedGetDocument, emit, clock, budgetMs}) {
    if (!request || typeof request.documentId !== "string" || !request.documentId
        || typeof managedGetDocument !== "function" || typeof emit !== "function")
      throw new Error("NORMAL_CALLSITE_BINDING_INVALID");
    return adapter.readAndRelease(request,
      () => managedGetDocument({document_id:request.documentId}),
      emit, {clock, budgetMs});
  };
};
