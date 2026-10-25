import QtQuick
import Quickshell.Io
import qs.Services.UI

Item {
    property var pluginApi: null

    IpcHandler {
        target: "plugin:hypr-scatter"

        function panelOpen(): bool {
            return PanelService.openedPanel !== null;
        }
    }
}
