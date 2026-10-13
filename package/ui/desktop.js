// DSM desktop integration: an app window that hosts the Homeward page (index.html).
// The page itself is plain HTML/JS and talks to api.cgi, so it does not depend on
// DSM's internal widget library.
Ext.namespace("SYNO.SDS.Homeward");

Ext.define("SYNO.SDS.Homeward.Instance", {
    extend: "SYNO.SDS.AppInstance",
    appWindowName: "SYNO.SDS.Homeward.MainWindow",
    constructor: function () {
        this.callParent(arguments);
    }
});

Ext.define("SYNO.SDS.Homeward.MainWindow", {
    extend: "SYNO.SDS.AppWindow",
    constructor: function (config) {
        this.appInstance = config.appInstance;
        const src = "webman/3rdparty/homeward/index.html?_=" + Date.now();
        this.callParent([Ext.apply({
            layout: "fit",
            cls: "syno-app-win",
            resizable: true,
            maximizable: true,
            minimizable: true,
            width: 980,
            height: 640,
            minWidth: 720,
            minHeight: 480,
            html: '<iframe src="' + src + '" title="Homeward" ' +
                'style="width:100%;height:100%;border:0;margin:0;display:block"></iframe>'
        }, config)]);
    }
});
