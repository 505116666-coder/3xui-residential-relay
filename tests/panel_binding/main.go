// Reproduces the v3.7.0 form-binding failure with its Gin version.
// Source contract: https://github.com/MHSanaei/3x-ui/blob/v3.7.0/internal/web/controller/inbound.go
// This checks HTTP form binding, not a complete running panel or a real VPS.
// Run manually with Go: cd tests/panel_binding && go run .
package main

import (
 "fmt"
 "net/http/httptest"
 "net/url"
 "strings"
 "github.com/gin-gonic/gin"
)
// Same form field types as 3X-UI v3.7.0 model.Inbound and setInboundEnable.
// Exercise Gin itself, not a Python approximation of Go form binding.
type ClientTraffic struct { Id int `json:"id"`; Email string `json:"email"`; Enable bool `json:"enable"` }
type Inbound struct {
 Id int `form:"id"`
 Port int `form:"port"`
 Enable bool `form:"enable"`
 Settings string `form:"settings"`
 StreamSettings string `form:"streamSettings"`
 Sniffing string `form:"sniffing"`
 ClientStats []ClientTraffic `form:"clientStats"`
}
func call(path string, data url.Values) (int,string) {
 r:=gin.New()
 r.POST("/update",func(c *gin.Context){var f Inbound;if err:=c.ShouldBind(&f);err!=nil {c.String(400,"%s",err);return};c.String(200,"ok")})
 r.POST("/setEnable",func(c *gin.Context){var f struct {Enable bool `json:"enable" form:"enable"`};if err:=c.ShouldBind(&f);err!=nil {c.String(400,"%s",err);return};if !f.Enable {c.String(400,"not enabled");return};c.String(200,"enabled")})
 w:=httptest.NewRecorder();req:=httptest.NewRequest("POST",path,strings.NewReader(data.Encode()));req.Header.Set("Content-Type","application/x-www-form-urlencoded");r.ServeHTTP(w,req);return w.Code,w.Body.String()
}
func main(){
 gin.SetMode(gin.TestMode)
 data:=url.Values{"id":{"4"},"port":{"54487"},"enable":{"True"},"settings":{"{\"clients\":[{\"id\":\"test-uuid\",\"enable\":true}]}"},"streamSettings":{"{\"network\":\"tcp\"}"},"sniffing":{"{\"enabled\":false}"},"clientStats":{"[{\"id\":1,\"email\":\"test\",\"enable\":true}]"}}
 code,body:=call("/update",data);if code!=400 {panic(fmt.Sprintf("expected old payload to fail, got %d %s",code,body))};fmt.Println("Old update payload rejected by Gin:",body)
 delete(data,"clientStats");code,body=call("/update",data);if code!=200 {panic(body)};fmt.Println("Without read-only clientStats: accepted")
 code,body=call("/setEnable",url.Values{"enable":{"True"}});if code!=200 {panic(body)};fmt.Println("Dedicated setEnable payload: accepted, enable=true")
}
