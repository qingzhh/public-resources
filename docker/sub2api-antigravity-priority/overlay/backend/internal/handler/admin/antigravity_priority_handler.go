package admin

import (
	"net/http"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

func (h *AntigravityOAuthHandler) SetPriorityPlugin(plugin *service.AntigravityPriorityService) {
	h.priorityPlugin = plugin
}

func (h *AntigravityOAuthHandler) PriorityAPI(c *gin.Context) {
	if h.priorityPlugin == nil {
		c.AbortWithStatus(http.StatusServiceUnavailable)
		return
	}
	h.priorityPlugin.Handler().ServeHTTP(c.Writer, c.Request)
}

func (h *AntigravityOAuthHandler) PriorityUI(c *gin.Context) {
	if h.priorityPlugin == nil {
		c.AbortWithStatus(http.StatusServiceUnavailable)
		return
	}
	h.priorityPlugin.UIHandler().ServeHTTP(c.Writer, c.Request)
}
